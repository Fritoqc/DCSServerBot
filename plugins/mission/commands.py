import asyncio
import discord
import importlib
import os
import psycopg
import random
import re
import traceback

from contextlib import suppress
from core import utils, Plugin, Report, Status, Server, Coalition, Channel, Player, PluginRequiredError, MizFile, \
    Group, ReportEnv, command, PlayerType, DataObjectFactory, Member, DEFAULT_TAG, get_translation, \
    UnsupportedMizFileException
from datetime import datetime, timezone
from discord import Interaction, app_commands, SelectOption
from discord.app_commands import Range
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput
from io import BytesIO
from pathlib import Path
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional, Union, Literal, Type

from .listener import MissionEventListener
from .upload import MissionUploadHandler
from .views import ServerView, PresetView, InfoView

# ruamel YAML support
from ruamel.yaml import YAML
yaml = YAML()

_ = get_translation(__name__.split('.')[1])


async def mizfile_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    if not await interaction.command._check_can_run(interaction):
        return []
    try:
        server: Server = await utils.ServerTransformer().transform(
            interaction, utils.get_interaction_param(interaction, 'server'))
        if not server:
            return []
        base_dir = await server.get_missions_dir()
        ignore = ['.dcssb']
        if server.locals.get('ignore_dirs'):
            ignore.extend(server.locals['ignore_dirs'])
        installed_missions = [os.path.expandvars(x) for x in await server.getMissionList()]
        exp_base, file_list = await server.node.list_directory(base_dir, pattern="*.miz", traverse=True, ignore=ignore)
        choices: list[app_commands.Choice[int]] = [
            app_commands.Choice(name=os.path.relpath(x, exp_base)[:-4], value=os.path.relpath(x, exp_base))
            for x in file_list
            if x not in installed_missions and os.path.join(os.path.dirname(x), '.dcssb', os.path.basename(
                x)) not in installed_missions and current.casefold() in os.path.relpath(x, base_dir).casefold()
        ]
        return choices[:25]
    except Exception as ex:
        interaction.client.log.exception(ex)
        return []

async def orig_mission_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    if not await interaction.command._check_can_run(interaction):
        return []
    try:
        server: Server = await utils.ServerTransformer().transform(interaction,
                                                                   utils.get_interaction_param(interaction, 'server'))
        if not server:
            return []
        _, file_list = await server.node.list_directory(await server.get_missions_dir(), pattern='*.orig',
                                                        traverse=True)
        orig_files = [os.path.basename(x)[:-9] for x in file_list]
        choices: list[app_commands.Choice[int]] = [
            app_commands.Choice(name=os.path.basename(x)[:-4], value=idx)
            for idx, x in enumerate(await server.getMissionList())
            if os.path.basename(x)[:-4] in orig_files and (not current or current.casefold() in x[:-4].casefold())
        ]
        return choices[:25]
    except Exception as ex:
        interaction.client.log.exception(ex)
        return []


async def presets_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not await interaction.command._check_can_run(interaction):
        return []
    try:
        choices: list[app_commands.Choice[str]] = [
            app_commands.Choice(name=x.name[:-5], value=str(x))
            for x in Path(interaction.client.node.config_dir).glob('presets*.yaml')
            if not current or current.casefold() in x.name[:-5].casefold()
        ]
        return choices[:25]
    except Exception as ex:
        interaction.client.log.exception(ex)
        return []


class Mission(Plugin[MissionEventListener]):

    def __init__(self, bot: DCSServerBot, listener: Type[MissionEventListener] = None):
        super().__init__(bot, listener)
        self.update_channel_name.add_exception_type(AttributeError)
        self.update_channel_name.start()
        self.afk_check.start()
        self.check_for_unban.start()
        self.expire_token.add_exception_type(psycopg.DatabaseError)
        self.expire_token.start()
        if self.bot.locals.get('autorole', {}):
            self.check_roles.start()

    async def cog_unload(self):
        if self.bot.locals.get('autorole', {}):
            self.check_roles.stop()
        self.expire_token.cancel()
        self.check_for_unban.cancel()
        self.afk_check.cancel()
        self.update_channel_name.cancel()
        await super().cog_unload()

    async def migrate(self, new_version: str, conn: Optional[psycopg.AsyncConnection] = None) -> None:
        function_name = f"migrate_{new_version.replace('.', '_')}"
        migrate_module = importlib.import_module('.migrate', package=__package__)
        migrate_function = getattr(migrate_module, function_name, None)
        if callable(migrate_function):
            await migrate_function(self)

    async def rename(self, conn: psycopg.AsyncConnection, old_name: str, new_name: str):
        await conn.execute('UPDATE missions SET server_name = %s WHERE server_name = %s', (new_name, old_name))

    async def prune(self, conn: psycopg.AsyncConnection, *, days: int = -1, ucids: list[str] = None,
                    server: Optional[str] = None) -> None:
        self.log.debug('Pruning Mission ...')
        if days > -1:
            # noinspection PyTypeChecker
            await conn.execute(f"""
                DELETE FROM missions 
                WHERE mission_end < (DATE((now() AT TIME ZONE 'utc')) - interval '{days} days')
            """)
        if server:
            await conn.execute("DELETE FROM missions WHERE server_name = %s", (server, ))
        self.log.debug('Mission pruned.')

    async def update_ucid(self, conn: psycopg.AsyncConnection, old_ucid: str, new_ucid: str) -> None:
        await conn.execute("""
            UPDATE bans SET ucid = %s WHERE ucid = %s AND NOT EXISTS (SELECT 1 FROM bans WHERE ucid = %s)
        """, (new_ucid, old_ucid, new_ucid))

    # New command group "/mission"
    mission = Group(name="mission", description=_("Commands to manage a DCS mission"))

    @mission.command(description=_('Info about the running mission'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS')
    async def info(self, interaction: Interaction, server: app_commands.Transform[Server, utils.ServerTransformer]):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        report = Report(self.bot, self.plugin_name, 'serverStatus.json')
        env: ReportEnv = await report.render(server=server)
        try:
            file = discord.File(fp=env.buffer, filename=env.filename) if env.filename else discord.utils.MISSING
            await interaction.followup.send(embed=env.embed, file=file, ephemeral=ephemeral)
        finally:
            if env.buffer:
                env.buffer.close()

    @mission.command(description=_('Manage the active mission'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def manage(self, interaction: Interaction, server: app_commands.Transform[Server, utils.ServerTransformer(
                       status=[Status.RUNNING, Status.PAUSED, Status.STOPPED])]):
        view = ServerView(server)
        embed = await view.render(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(embed=embed, view=view, ephemeral=utils.get_ephemeral(interaction))
        try:
            await view.wait()
        finally:
            await interaction.delete_original_response()

    @mission.command(description=_('Information about a specific airport'))
    @utils.app_has_role('DCS')
    @app_commands.guild_only()
    @app_commands.rename(idx=_('airport'))
    @app_commands.describe(idx=_('Airport for ATIS information'))
    @app_commands.autocomplete(idx=utils.airbase_autocomplete)
    async def atis(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(
                       status=[Status.RUNNING, Status.PAUSED])],
                   idx: int):
        if server.status not in [Status.RUNNING, Status.PAUSED]:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Server {} is not running.").format(server.display_name),
                                                    ephemeral=True)
            return
        # noinspection PyUnresolvedReferences
        await interaction.response.defer()
        airbase = server.current_mission.airbases[idx]
        data = await server.send_to_dcs_sync({
            "command": "getWeatherInfo",
            "x": airbase['position']['x'],
            "y": airbase['position']['y'],
            "z": airbase['position']['z']
        })
        report = Report(self.bot, self.plugin_name, 'atis.json')
        env = await report.render(airbase=airbase, data=data, server=server)
        await interaction.followup.send(embed=env.embed)

    @mission.command(description=_('Shows briefing of the active mission'))
    @utils.app_has_role('DCS')
    @app_commands.guild_only()
    async def briefing(self, interaction: discord.Interaction,
                       server: app_commands.Transform[Server, utils.ServerTransformer(
                           status=[Status.RUNNING, Status.PAUSED])]):
        async def read_passwords() -> dict:
            async with self.apool.connection() as conn:
                cursor = await conn.execute('SELECT blue_password, red_password FROM servers WHERE server_name = %s',
                                            (server.name,))
                row = await cursor.fetchone()
                return {"blue": row[0], "red": row[1]}

        if server.status not in [Status.RUNNING, Status.PAUSED]:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Server {} is not running.").format(server.display_name),
                                                    ephemeral=True)
            return
        # noinspection PyUnresolvedReferences
        await interaction.response.defer()
        mission_info = await server.send_to_dcs_sync({
            "command": "getMissionDetails"
        })
        mission_info['passwords'] = await read_passwords()
        report = Report(self.bot, self.plugin_name, 'briefing.json')
        env = await report.render(mission_info=mission_info, server_name=server.name, interaction=interaction)
        await interaction.followup.send(embed=env.embed)

    @mission.command(description=_('Restarts the current active mission\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def restart(self, interaction: discord.Interaction,
                      server: app_commands.Transform[Server, utils.ServerTransformer(
                          status=[Status.RUNNING, Status.PAUSED, Status.STOPPED])],
                      delay: Optional[int] = 120, reason: Optional[str] = None, run_extensions: Optional[bool] = True,
                      use_orig: Optional[bool] = True):
        await self._restart(interaction, server, delay, reason, run_extensions, use_orig, rotate=False)

    @mission.command(description=_('Rotates to the next mission\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def rotate(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer(
                          status=[Status.RUNNING, Status.PAUSED, Status.STOPPED])],
                     delay: Optional[int] = 120, reason: Optional[str] = None, run_extensions: Optional[bool] = True,
                     use_orig: Optional[bool] = True):
        await self._restart(interaction, server, delay, reason, run_extensions, use_orig, rotate=True)

    async def _restart(self, interaction: discord.Interaction,
                       server: app_commands.Transform[Server, utils.ServerTransformer(
                          status=[Status.RUNNING, Status.PAUSED, Status.STOPPED])],
                       delay: Optional[int] = 120, reason: Optional[str] = None, run_extensions: Optional[bool] = True,
                       use_orig: Optional[bool] = True, rotate: Optional[bool] = False):
        what = "restart" if not rotate else "rotate"
        actions = {
            "restart": "restarted",
            "rotate": "rotated",
        }
        ephemeral = utils.get_ephemeral(interaction)
        if server.status not in [Status.RUNNING, Status.PAUSED, Status.STOPPED]:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("Can't restart server {server} as it is {status}!").format(server=server.display_name,
                                                                             status=server.status.name), ephemeral=True)
            return
        if server.restart_pending and not await utils.yn_question(
                interaction, _('A restart is currently pending.\n'
                               'Would you still like to {} the mission?').format(_(what)),
                ephemeral=ephemeral):
            return
        else:
            server.on_empty = dict()
        if server.is_populated():
            result = await utils.populated_question(
                interaction, _("Do you really want to {} the mission?").format(_(what)), ephemeral=ephemeral)
            if not result:
                return
            elif result == 'later':
                server.on_empty = {
                    "command": what,
                    "user": interaction.user,
                    "run_extensions": run_extensions,
                    "use_orig": use_orig
                }
                server.restart_pending = True
                await interaction.followup.send(_('Mission will {}, when server is empty.').format(_(what)),
                                                ephemeral=ephemeral)
                return

        server.restart_pending = True
        # noinspection PyUnresolvedReferences
        if not interaction.response.is_done():
            # noinspection PyUnresolvedReferences
            await interaction.response.defer(ephemeral=ephemeral)
        if server.is_populated():
            if delay > 0:
                message = _("!!! Mission will be {what} in {when}!!!").format(what=_(actions.get(what)),
                                                                              when=utils.format_time(delay))
            else:
                message = _("!!! Mission will be {} NOW !!!").format(_(actions.get(what)))
            # have we got a message to present to the users?
            if reason:
                message += _(' Reason: {}').format(reason)

            msg = await interaction.followup.send(
                _('Mission will be {what} in {when} (warning users before)...').format(what=_(actions.get(what)),
                                                                                       when=utils.format_time(delay)),
                ephemeral=ephemeral)
            await server.sendPopupMessage(Coalition.ALL, message, sender=interaction.user.display_name)
            await asyncio.sleep(delay)
            await msg.delete()
        try:
            msg = await interaction.followup.send(_('Mission will {} now, please wait ...').format(_(what)),
                                                  ephemeral=ephemeral)
            if not server.locals.get('mission_rewrite', True) and server.status != Status.STOPPED:
                await server.stop()
            if rotate:
                await server.loadNextMission(modify_mission=run_extensions, use_orig=use_orig)
            else:
                await server.restart(modify_mission=run_extensions)
            await self.bot.audit(f'{actions.get(what)} mission', server=server, user=interaction.user)
            await msg.delete()
            await interaction.followup.send(_("Mission {}.").format(_(actions.get(what))), ephemeral=ephemeral)
        except (TimeoutError, asyncio.TimeoutError):
            await interaction.followup.send(
                _("Timeout while the mission {what}.\n"
                  "Please check with {command}, if the mission is running.").format(
                    what=_(actions.get(what)),
                    command=(await utils.get_command(self.bot, group='mission', name='info')).mention
                ), ephemeral=ephemeral)

    async def _load(self, interaction: discord.Interaction, server: Server, mission: Optional[Union[int, str]] = None,
                    run_extensions: Optional[bool] = False, use_orig: Optional[bool] = True):
        ephemeral = utils.get_ephemeral(interaction)
        if server.status not in [Status.RUNNING, Status.PAUSED, Status.STOPPED]:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("Can't load mission on server {server} as it is {status}!").format(
                    server=server.display_name, status=server.status.name), ephemeral=True)
            return
        if server.restart_pending and not await utils.yn_question(
                interaction,
                _('A restart is currently pending.\nWould you still like to {} the mission?').format(_("change")),
                ephemeral=ephemeral
        ):
            return
        else:
            server.on_empty = dict()

        if server.is_populated():
            result = await utils.populated_question(
                interaction,
                _("Do you really want to {} the mission?").format(_("change")),
                ephemeral=ephemeral
            )
            if not result:
                return
        else:
            result = "yes"

        # noinspection PyUnresolvedReferences
        if not interaction.response.is_done():
            # noinspection PyUnresolvedReferences
            await interaction.response.defer(ephemeral=ephemeral)
        if isinstance(mission, int):
            mission_id = mission
            mission = (await server.getMissionList())[mission_id]
        elif isinstance(mission, str):
            try:
                mission = os.path.join(await server.get_missions_dir(), mission)
                mission_id = (await server.getMissionList()).index(mission)
            except ValueError:
                mission_id = None
        else:
            await interaction.followup.send(_('You need to provide a mission!'), ephemeral=True)
            return
        if server.current_mission and mission == server.current_mission.filename:
            if result == 'later':
                server.on_empty = {
                    "command": "restart",
                    "user": interaction.user,
                    "run_extensions": run_extensions,
                    "use_orig": use_orig
                }
                await interaction.followup.send(_('Mission will {}, when server is empty.').format(_('restart')),
                                                ephemeral=ephemeral)
            else:
                await server.restart(modify_mission=run_extensions)
                await interaction.followup.send(_('Mission {}.').format(_('restarted')), ephemeral=ephemeral)
        else:
            name = os.path.basename(mission[:-4])
            if mission_id and result == 'later':
                # make sure, we load that mission, independently on what happens to the server
                await server.setStartIndex(mission_id + 1)
                server.on_empty = {
                    "command": "load",
                    "mission_id": mission_id + 1,
                    "run_extensions": run_extensions,
                    "use_orig": use_orig,
                    "user": interaction.user
                }
                await interaction.followup.send(
                    _('Mission {} will be loaded when server is empty or on the next restart.').format(name),
                    ephemeral=ephemeral)
            else:
                msg = await interaction.followup.send(_('Loading mission {} ...').format(utils.escape_string(name)),
                                                      ephemeral=ephemeral)
                try:
                    if not server.locals.get('mission_rewrite', True) and server.status != Status.STOPPED:
                        await server.stop()
                    if not await server.loadMission(mission, modify_mission=run_extensions, use_orig=use_orig):
                        await msg.edit(content=_('Mission {} NOT loaded. '
                                                 'Check that you have installed the pre-requisites (terrains, mods).'
                                                 ).format(name))
                    else:
                        message = _('Mission {} loaded.').format(name)
                        if mission_id is None:
                            message += _('\nThis mission is NOT in the mission list and will not auto-load on server '
                                         'or mission restarts.\n'
                                         'If you want it to auto-load, use {}').format(
                                (await utils.get_command(self.bot, group='mission', name='add')).mention)
                        await msg.edit(content=message)
                        await self.bot.audit(f"loaded mission {utils.escape_string(name)}", server=server,
                                             user=interaction.user)
                except (TimeoutError, asyncio.TimeoutError):
                    await msg.edit(content=_('Timeout while loading mission {}!').format(name))
                except UnsupportedMizFileException as ex:
                    await msg.edit(content=ex)

    @mission.command(description=_('Loads a mission\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.rename(mission_id="mission")
    @app_commands.describe(use_orig="Change the mission based on the original uploaded mission file.")
    @app_commands.autocomplete(mission_id=utils.mission_autocomplete)
    @app_commands.autocomplete(alt_mission=mizfile_autocomplete)
    async def load(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(
                       status=[Status.STOPPED, Status.RUNNING, Status.PAUSED])],
                   mission_id: Optional[int] = None, alt_mission: Optional[str] = None,
                   run_extensions: Optional[bool] = True, use_orig: Optional[bool] = True):
        await self._load(
            interaction,
            server,
            mission_id if mission_id is not None else alt_mission,
            run_extensions,
            use_orig
        )

    @mission.command(description=_('Adds a mission to the list\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.autocomplete(path=mizfile_autocomplete)
    async def add(self, interaction: discord.Interaction,
                  server: app_commands.Transform[Server, utils.ServerTransformer], path: str,
                  autostart: Optional[bool] = False):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)

        path = os.path.normpath(os.path.join(await server.get_missions_dir(), path))
        new_mission_list = await server.addMission(path, autostart=autostart)
        name = os.path.basename(path)
        await interaction.followup.send(_('Mission "{}" added.').format(utils.escape_string(name)), ephemeral=ephemeral)
        mission_id = new_mission_list.index(path)
        if server.status not in [Status.RUNNING, Status.PAUSED, Status.STOPPED] or \
                not await utils.yn_question(interaction, _('Do you want to load this mission?'),
                                            ephemeral=ephemeral):
            return
        await self._load(interaction, server, mission_id, False)

    @mission.command(description=_('Deletes a mission from the list\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.rename(mission_id="mission")
    @app_commands.autocomplete(mission_id=utils.mission_autocomplete)
    async def delete(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer],
                     mission_id: int):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        missions = await server.getMissionList()
        if mission_id >= len(missions):
            await interaction.followup.send(_("No mission found."))
            return
        filename = missions[mission_id]
        if server.status in [Status.RUNNING, Status.PAUSED, Status.STOPPED] and server.current_mission and \
                filename == server.current_mission.filename:
            await interaction.followup.send(_("You can't delete the running mission."), ephemeral=True)
            return
        name = filename[:-4]

        if await utils.yn_question(interaction,
                                   _('Delete mission "{}" from the mission list?').format(os.path.basename(name)),
                                   ephemeral=ephemeral):
            try:
                await server.deleteMission(mission_id + 1)
                await interaction.followup.send(_('Mission "{}" removed from list.').format(os.path.basename(name)),
                                                ephemeral=ephemeral)
                if await utils.yn_question(interaction,
                                           _('Delete "{}" also from disk?').format(os.path.basename(filename)),
                                           ephemeral=ephemeral):
                    try:
                        await server.node.remove_file(filename)
                        if '.dcssb' in filename:
                            secondary = filename
                            primary = filename.replace(os.path.sep + '.dcssb', '')
                            await server.node.remove_file(primary)
                        else:
                            secondary = os.path.join(os.path.dirname(filename), '.dcssb', os.path.basename(filename))
                            await server.node.remove_file(secondary)
                        await server.node.remove_file(secondary + '.orig')
                        await interaction.followup.send(_('Mission "{}" deleted.').format(os.path.basename(filename)),
                                                        ephemeral=ephemeral)
                    except FileNotFoundError:
                        await interaction.followup.send(
                            _('Mission "{}" was already deleted.').format(os.path.basename(filename)),
                            ephemeral=ephemeral)
                await self.bot.audit(_("deleted mission {}").format(name), user=interaction.user)
            except (TimeoutError, asyncio.TimeoutError):
                await interaction.followup.send(_("Timeout while deleting mission.\n"
                                                  "Please reconfirm that the deletion was successful."),
                                                ephemeral=ephemeral)

    @mission.command(description=_('Pauses the current running mission'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def pause(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])]):
        ephemeral = utils.get_ephemeral(interaction)
        if server.status == Status.RUNNING:
            # noinspection PyUnresolvedReferences
            await interaction.response.defer(thinking=True, ephemeral=ephemeral)
            await server.current_mission.pause()
            await interaction.followup.send(_('Mission on server "{}" paused.').format(server.display_name),
                                            ephemeral=ephemeral)
        else:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_('Server {} is not running.').format(server.display_name),
                                                    ephemeral=ephemeral)

    @mission.command(description=_('Resumes the running mission'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def unpause(self, interaction: discord.Interaction,
                      server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.PAUSED])]):
        ephemeral = utils.get_ephemeral(interaction)
        if server.status == Status.PAUSED:
            # noinspection PyUnresolvedReferences
            await interaction.response.defer(thinking=True, ephemeral=ephemeral)
            await server.current_mission.unpause()
            await interaction.followup.send(_('Mission on server "{}" resumed.').format(server.display_name),
                                            ephemeral=ephemeral)
        elif server.status == Status.RUNNING:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_('Server "{}" is not paused.').format(server.display_name),
                                                    ephemeral=ephemeral)
        else:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("Server {server} is {status}, can't unpause.").format(server=server.display_name,
                                                                        status=server.status.name),
                ephemeral=ephemeral)

    @mission.command(description=_('Modify mission with a preset\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.autocomplete(presets_file=presets_autocomplete)
    @app_commands.describe(presets_file=_('Chose an alternate presets file'))
    @app_commands.describe(use_orig="Change the mission based on the original uploaded mission file.")
    async def modify(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer(
                         status=[Status.RUNNING, Status.PAUSED, Status.STOPPED, Status.SHUTDOWN])],
                     presets_file: Optional[str] = None, use_orig: Optional[bool] = True):
        ephemeral = utils.get_ephemeral(interaction)
        if presets_file is None:
            presets_file = os.path.join(self.node.config_dir, 'presets.yaml')
        try:
            with open(presets_file, mode='r', encoding='utf-8') as infile:
                presets = yaml.load(infile)
        except FileNotFoundError:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _('No presets available, please configure them in {}.').format(presets_file), ephemeral=True)
            return
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        try:
            try:
                next((x for x in presets.values() if 'terrain' in x or 'terrains' in x), None)
                terrain = await server.get_current_mission_theatre()
            except StopIteration:
                terrain = ''
            options = [
                discord.SelectOption(label=k)
                for k, v in presets.items()
                if not isinstance(v, dict) or (
                        not v.get('hidden', False)
                        and v.get('terrain', terrain) == terrain
                        and terrain in v.get('terrains', [terrain])
                )
            ]
        except AttributeError:
            await interaction.followup.send(
                _("There is an error in your {}. Please check the file structure.").format(presets_file),
                ephemeral=True)
            return
        if len(options) > 25:
            self.log.warning("You have more than 25 presets created, you can only choose from 25!")
        elif not options:
            await interaction.followup.send(_("There are no presets to chose from."), ephemeral=True)

        result = None
        if server.status in [Status.PAUSED, Status.RUNNING]:
            question = _('Do you want to restart the server for a mission change?')
            if server.is_populated():
                result = await utils.populated_question(interaction, question, ephemeral=ephemeral)
            else:
                result = await utils.yn_question(interaction, question, ephemeral=ephemeral)
            if not result:
                return

        view = PresetView(options[:25])
        # noinspection PyUnresolvedReferences
        if interaction.response.is_done():
            msg = await interaction.followup.send(view=view, ephemeral=ephemeral)
        else:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(view=view, ephemeral=ephemeral)
            msg = await interaction.original_response()
        try:
            if await view.wait() or view.result is None:
                return
        finally:
            try:
                await msg.delete()
            except discord.NotFound:
                pass
        if result == 'later':
            server.on_empty = {
                "command": "preset",
                "preset": view.result,
                "use_orig": use_orig,
                "user": interaction.user
            }
            server.restart_pending = True
            await interaction.followup.send(_('Mission will be changed when server is empty.'), ephemeral=ephemeral)
        else:
            server.on_empty = dict()
            startup = False
            msg = await interaction.followup.send(_('Changing mission ...'), ephemeral=ephemeral)
            if not server.locals.get('mission_rewrite', True) and server.status != Status.STOPPED:
                await server.stop()
                startup = True
            filename = await server.get_current_mission_file()
            new_filename = await server.modifyMission(
                filename,
                [utils.get_preset(self.node, x) for x in view.result],
                use_orig=use_orig
            )
            message = _('The following preset were applied: {}.').format(','.join(view.result))
            if new_filename != filename:
                self.log.info(f"  => {message}")
                self.log.info(f"  => New mission written: {new_filename}")
                await server.replaceMission(int(server.settings['listStartIndex']), new_filename)
            else:
                self.log.info(f"  => Mission {filename} overwritten.")
            if startup or server.status not in [Status.STOPPED, Status.SHUTDOWN]:
                try:
                    # if the filename has not changed, we can just restart the running mission
                    if filename == new_filename:
                        await server.restart(modify_mission=False)
                    # otherwise we load the new mission
                    else:
                        await server.loadMission(new_filename, modify_mission=False)
                    message += _('\nMission reloaded.')
                    await self.bot.audit("changed preset {}".format(','.join(view.result)), server=server,
                                         user=interaction.user)
                    await msg.delete()
                except (TimeoutError, asyncio.TimeoutError):
                    message = _("Timeout during restart of mission!\n"
                                "Please check, if the mission is running or if it somehow got corrupted.")
            await interaction.followup.send(message, ephemeral=ephemeral)

    @mission.command(description=_('Save mission preset\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def save_preset(self, interaction: discord.Interaction,
                          server: app_commands.Transform[Server, utils.ServerTransformer(
                              status=[Status.RUNNING, Status.PAUSED, Status.STOPPED])],
                          name: str):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        miz = await asyncio.to_thread(MizFile, server.current_mission.filename)
        config_file = os.path.join(self.node.config_dir, 'presets.yaml')
        if os.path.exists(config_file):
            with open(config_file, mode='r', encoding='utf-8') as infile:
                presets = yaml.load(infile)
        else:
            presets = {}
        if name in presets and \
                not await utils.yn_question(interaction,
                                            _('Do you want to overwrite the existing preset "{}"?').format(name),
                                            ephemeral=ephemeral):
            return
        presets[name] = {
            "start_time": miz.start_time,
            "date": miz.date.strftime('%Y-%m-%d'),
            "temperature": miz.temperature,
            "clouds": miz.clouds,
            "wind": miz.wind,
            "groundTurbulence": miz.groundTurbulence,
            "enable_dust": miz.enable_dust,
            "dust_density": miz.dust_density if miz.enable_dust else 0,
            "qnh": miz.qnh,
            "enable_fog": miz.enable_fog,
            "fog": miz.fog if miz.enable_fog else {"thickness": 0, "visibility": 0},
            "halo": miz.halo
        }
        with open(config_file, mode='w', encoding='utf-8') as outfile:
            yaml.dump(presets, outfile)
        # noinspection PyUnresolvedReferences
        await interaction.followup.send(_('Preset "{}" added.').format(name), ephemeral=ephemeral)

    @mission.command(description=_('Rollback to the original mission file after any modifications'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.rename(mission_id="mission")
    @app_commands.autocomplete(mission_id=orig_mission_autocomplete)
    async def rollback(self, interaction: discord.Interaction,
                       server: app_commands.Transform[Server, utils.ServerTransformer], mission_id: int):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        missions = await server.getMissionList()
        if mission_id >= len(missions):
            await interaction.followup.send(_("No mission found."), ephemeral=True)
            return
        filename = missions[mission_id]
        if server.status in [Status.RUNNING, Status.PAUSED] and filename == server.current_mission.filename:
            await interaction.followup.send(_("Please stop your server first to rollback the running mission."),
                                            ephemeral=True)
            return
        if '.dcssb' in filename:
            new_file = os.path.join(os.path.dirname(filename).replace('.dcssb', ''),
                                    os.path.basename(filename))
            orig_file = filename + '.orig'
        else:
            new_file = filename
            orig_file = os.path.join(os.path.dirname(filename), '.dcssb', os.path.basename(filename)) + '.orig'
        try:
            await server.node.rename_file(orig_file, new_file, force=True)
        except FileNotFoundError:
            # we should never be here, but just in case
            await interaction.followup.send(_('No ".orig" file there, the mission was never changed.'),
                                            ephemeral=True)
            return
        if new_file != filename:
            await server.replaceMission(mission_id + 1, new_file)
        await interaction.followup.send(_("Mission {} has been rolled back.").format(os.path.basename(filename)[:-4]),
                                        ephemeral=ephemeral)

    @mission.command(description=_('Sets fog in the running mission'))
    @app_commands.guild_only()
    @app_commands.describe(thickness=_("Thickness of the fog [100-5000]m, to disable, set 0."))
    @app_commands.describe(visibility=_("Visibility of the fog [100-100000]m, to disable, set 0."))
    @utils.app_has_role('DCS Admin')
    @utils.app_has_dcs_version("2.9.10")
    async def fog(self, interaction: discord.Interaction,
                  server: app_commands.Transform[Server, utils.ServerTransformer(
                      status=[Status.RUNNING, Status.PAUSED])],
                  thickness: Optional[app_commands.Range[int, 0, 5000]] = None,
                  visibility: Optional[app_commands.Range[int, 0, 100000]] = None):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        if thickness is None and visibility is None:
            ret = await server.send_to_dcs_sync({
                "command": "getFog"
            })
        else:
            if thickness and thickness < 100:
                await interaction.followup.send(_("Thickness has to be in the range 100-5000"))
                return
            if visibility and visibility < 100:
                await interaction.followup.send(_("Visibility has to be in the range 100-100000"))
                return
            ret = await server.send_to_dcs_sync({
                "command": "setFog",
                "thickness": thickness if thickness is not None else -1,
                "visibility": visibility if visibility is not None else -1
            })
        await interaction.followup.send(_("Current Fog Settings:\n- Thickness: {thickness:.2f}m\n- Visibility:\t{visibility:.2f}m").format(
            thickness=ret['thickness'], visibility=ret['visibility']), ephemeral=ephemeral)

    @mission.command(description=_('Runs a fog animation'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.autocomplete(presets_file=presets_autocomplete)
    @utils.app_has_dcs_version("2.9.10")
    async def fog_animation(self, interaction: discord.Interaction,
                            server: app_commands.Transform[Server, utils.ServerTransformer(
                                status=[Status.RUNNING, Status.PAUSED])],
                            presets_file: Optional[str] = None):
        ephemeral = utils.get_ephemeral(interaction)
        if presets_file is None:
            presets_file = os.path.join(self.node.config_dir, 'presets.yaml')
        try:
            with open(presets_file, mode='r', encoding='utf-8') as infile:
                presets = yaml.load(infile)
        except FileNotFoundError:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _('No presets available, please configure them in {}.').format(presets_file), ephemeral=True)
            return
        try:
            options = [
                discord.SelectOption(label=k)
                for k, v in presets.items()
                if not v.get('hidden', False) and v.get('fog') and
                   (v['fog'].get('mode', None) == 'manual' or all(isinstance(y, int) for y in v['fog'].keys()))
            ]
        except AttributeError:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("There is an error in your {}. Please check the file structure.").format(presets_file),
                ephemeral=True)
            return
        if len(options) > 25:
            self.log.warning("You have more than 25 presets created, you can only choose from 25!")
        elif not options:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("There is no manual fog preset in your {}").format(presets_file),
                                                    ephemeral=True)
            return

        # select a preset
        view = PresetView(options[:25], multi=False)
        # noinspection PyUnresolvedReferences
        if interaction.response.is_done():
            msg = await interaction.followup.send(view=view, ephemeral=ephemeral)
        else:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(view=view, ephemeral=ephemeral)
            msg = await interaction.original_response()
        try:
            if await view.wait() or view.result is None:
                return
        finally:
            try:
                await msg.delete()
            except discord.NotFound:
                pass

        fog = utils.get_preset(self.node, view.result[0])['fog']
        fog.pop('mode', None)
        await server.send_to_dcs_sync(
            {
                'command': 'setFogAnimation',
                'values': [
                    (key, value["visibility"], value["thickness"])
                    for key, value in fog.items()
                ]
            })
        message = _('The following preset was applied: {}.').format(view.result[0])
        await interaction.followup.send(message, ephemeral=ephemeral)

    # New command group "/player"
    player = Group(name="player", description=_("Commands to manage DCS players"))

    @player.command(name='list', description=_('Lists the current players'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS')
    async def _list(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])]):
        if server.status != Status.RUNNING:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Server {} is not running.").format(server.display_name),
                                                    ephemeral=True)
            return
        report = Report(self.bot, self.plugin_name, 'players.json')
        env = await report.render(server=server, sides=utils.get_sides(interaction.client, interaction, server))
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(embed=env.embed, ephemeral=utils.get_ephemeral(interaction))

    @player.command(description=_('Kicks a player\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def kick(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                   player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)],
                   reason: Optional[str] = 'n/a') -> None:
        if not player:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player not found."), ephemeral=True)
            return
        await server.kick(player, reason)
        await self.bot.audit(f'kicked player {player.display_name} with reason "{reason}"', user=interaction.user)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(
            _("Player {name} (ucid={ucid}) kicked.").format(name=player.display_name, ucid=player.ucid),
            ephemeral=utils.get_ephemeral(interaction))

    @player.command(description=_('Bans an active player'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def ban(self, interaction: discord.Interaction,
                  server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                  player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)]):

        class BanModal(Modal, title=_("Ban Details")):
            reason = TextInput(label=_("Reason"), default=_("n/a"), max_length=80, required=False)
            period = TextInput(label=_("Days (empty = forever)"), required=False)

            async def on_submit(derived, interaction: discord.Interaction):
                days = int(derived.period.value) if derived.period.value else None
                await self.bus.ban(player.ucid, interaction.user.display_name, derived.reason.value, days)
                # noinspection PyUnresolvedReferences
                await interaction.response.send_message(
                    _("Player {} banned on all servers ").format(player.display_name) +
                    (_("for {} days.").format(days) if days else ""),
                    ephemeral=utils.get_ephemeral(interaction))
                await self.bot.audit(f'banned player {player.display_name} with reason "{derived.reason.value}"' +
                                     (f' for {days} days.' if days else ' permanently.'), user=interaction.user)
        if not player:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player not found."), ephemeral=True)
            return
        # noinspection PyUnresolvedReferences
        await interaction.response.send_modal(BanModal())

    @player.command(description=_('Moves a player to spectators\n'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def spec(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                   player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)],
                   reason: Optional[str] = 'n/a') -> None:
        if not player:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player not found."), ephemeral=True)
            return
        await server.move_to_spectators(player)
        if reason:
            await player.sendChatMessage(_("You have been moved to spectators. Reason: {}").format(reason),
                                         interaction.user.display_name)
        await self.bot.audit(f'moved player {player.name} to spectators with reason "{reason}".', user=interaction.user)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(_('Player "{}" moved to spectators.').format(player.name),
                                                ephemeral=utils.get_ephemeral(interaction))

    @player.command(description=_('List of AFK players'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def afk(self, interaction: discord.Interaction,
                  server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                  minutes: Optional[int] = 10):
        if server.status != Status.RUNNING:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Server {} is not running.").format(server.display_name),
                                                    ephemeral=True)
            return
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        afk: list[Player] = list()
        for s in self.bot.servers.values():
            if server and s != server:
                continue
            for ucid, dt in s.afk.items():
                player = s.get_player(ucid=ucid, active=True)
                if not player:
                    continue
                if (datetime.now(tz=timezone.utc) - dt).total_seconds() > minutes * 60:
                    afk.append(player)

        if afk:
            title = 'AFK Players'
            if server:
                title += f' on {server.name}'
            embed = discord.Embed(title=title, color=discord.Color.blue())
            embed.description = _('These players are AFK for more than {} minutes:').format(minutes)
            for player in sorted(afk, key=lambda x: x.server.name):
                embed.add_field(name=_('Name'), value=player.display_name)
                embed.add_field(name=_('Time'),
                                value=utils.format_time(int((datetime.now(timezone.utc) -
                                                             player.server.afk[player.ucid]).total_seconds())))
                if server:
                    embed.add_field(name='_ _', value='_ _')
                else:
                    embed.add_field(name=_('Server'), value=player.server.display_name)
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.followup.send(_("No player is AFK for more than {} minutes.").format(minutes),
                                            ephemeral=ephemeral)

    @player.command(description=_('Exempt player from AFK kicks'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def exempt(self, interaction: discord.Interaction,
                     user: app_commands.Transform[
                         Union[discord.Member, str], utils.UserTransformer(sel_type=PlayerType.PLAYER)
                     ],
                     server: Optional[app_commands.Transform[Server, utils.ServerTransformer]]):
        ephemeral = utils.get_ephemeral(interaction)
        if isinstance(user, discord.Member):
            ucid = await self.bot.get_ucid_by_member(user)
        else:
            ucid = user
        config_file = os.path.join(self.node.config_dir, 'servers.yaml')
        if not server:
            section = DEFAULT_TAG
        else:
            section = server.name
        data = yaml.load(Path(config_file).read_text(encoding='utf-8'))
        if section not in data:
            data[section] = {}
        if 'afk' not in data[section]:
            data[section]['afk'] = {}
        if 'exemptions' not in data[section]['afk']:
            data[section]['afk']['exemptions'] = {}
        if 'ucid' not in data[section]['afk']['exemptions']:
            data[section]['afk']['exemptions']['ucid'] = []
        if ucid not in data[section]['afk']['exemptions']['ucid']:
            if not await utils.yn_question(interaction,
                                           _("Do you want to permanently add this user to the AFK exemption list?"),
                                           ephemeral=ephemeral):
                await interaction.followup.send("Aborted.", ephemeral=ephemeral)
                return
            data[section]['afk']['exemptions']['ucid'].append(ucid)
            await interaction.followup.send(_("User added to the exemption list."), ephemeral=ephemeral)
        else:
            if not await utils.yn_question(interaction,
                                           _("Player is on the list already. Do you want to remove them?")):
                await interaction.followup.send(_("Aborted."), ephemeral=ephemeral)
                return
            data[section]['afk']['exemptions']['ucid'].remove(ucid)
            await interaction.followup.send(_("User removed from the exemption list."), ephemeral=ephemeral)
        with open(config_file, 'w', encoding='utf-8') as outfile:
            yaml.dump(data, outfile)

    @player.command(description=_('Sends a popup to a player\n'))
    @app_commands.guild_only()
    @utils.app_has_roles(['DCS Admin', 'GameMaster'])
    async def popup(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                    player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)],
                    message: str, time: Optional[Range[int, 1, 30]] = -1):
        if not player:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player not found."), ephemeral=True)
            return
        await player.sendPopupMessage(message, time, interaction.user.display_name)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(_('Message sent.'), ephemeral=utils.get_ephemeral(interaction))

    @player.command(description=_('Sends a chat message to a player\n'))
    @app_commands.guild_only()
    @utils.app_has_roles(['DCS Admin', 'GameMaster'])
    async def chat(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                   player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)], message: str):
        if not player:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player not found."), ephemeral=True)
            return
        await player.sendChatMessage(message, interaction.user.display_name)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(_('Message sent.'), ephemeral=utils.get_ephemeral(interaction))

    @player.command(description=_('Take a screenshot'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def screenshot(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                   player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)]) -> None:
        if not player:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player not found."), ephemeral=True)
            return
        if not server.settings.get('advanced', {}).get('server_can_screenshot'):
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Server can not take screenshots."), ephemeral=True)
            return
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        msg = await interaction.followup.send(_("Requesting screenshot ..."), ephemeral=ephemeral)
        try:
            old_screens = await player.getScreenshots()
            await player.makeScreenshot()
            timeout = 30 if server.node.locals.get('slow_system', False) else 10
            for i in range(1, timeout):
                await asyncio.sleep(1)
                new_screens = await player.getScreenshots()
                if len(new_screens) > len(old_screens):
                    break
            else:
                await msg.edit(content=_("Timeout while waiting for screenshot!"))
                return
        except (TimeoutError, asyncio.TimeoutError):
            await msg.edit(content=_("Timeout while waiting for screenshot!"))
            return
        key = new_screens[-1]
        # DCS 2.9.11+
        if 'screenshots' not in key:
            key = '/screenshots/' + key
        try:
            image_url = f"http://127.0.0.1:{server.instance.webgui_port}{key}"
            image_data = await server.node.read_file(image_url)
            file = discord.File(BytesIO(image_data), filename="screenshot.png")
            await msg.delete()
            embed = discord.Embed(color=discord.Color.blue(),
                                  title=_("Screenshot of Player {}").format(player.display_name))
            embed.set_image(url="attachment://screenshot.png")
            embed.add_field(name=_("Server"), value=server.display_name, inline=False)
            embed.add_field(name=_("Time"), value=f"<t:{int(datetime.now().timestamp())}>", inline=False)
            embed.add_field(name=_("Taken by"), value=interaction.user.display_name, inline=False)
            await interaction.followup.send(embed=embed, file=file, ephemeral=ephemeral)
        finally:
            await player.deleteScreenshot(key)

    watch = Group(name="watch", description="Commands to manage the watchlist")

    @watch.command(description=_('Puts a player onto the watchlist'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def add(self, interaction: discord.Interaction,
                  user: app_commands.Transform[Union[discord.Member, str], utils.UserTransformer(
                      sel_type=PlayerType.PLAYER, watchlist=False)], reason: str):
        if isinstance(user, discord.Member):
            ucid = await self.bot.get_ucid_by_member(user)
            if not ucid:
                # noinspection PyUnresolvedReferences
                await interaction.response.send_message(_("Member {} is not linked!").format(user.display_name))
                return
        else:
            ucid = user
        try:
            async with self.apool.connection() as conn:
                async with conn.transaction():
                    await conn.execute("INSERT INTO watchlist (player_ucid, reason, created_by) VALUES (%s, %s, %s)",
                                       (ucid, reason, interaction.user.display_name))
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("Player {} is now on the watchlist.").format(
                user.display_name if isinstance(user, discord.Member) else ucid),
                ephemeral=utils.get_ephemeral(interaction))
        except psycopg.errors.UniqueViolation:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("Player {} was already on the watchlist.").format(
                    user.display_name if isinstance(user, discord.Member) else ucid),
                ephemeral=utils.get_ephemeral(interaction))

    @watch.command(description=_('Removes a player from the watchlist'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def delete(self, interaction: discord.Interaction,
                     user: app_commands.Transform[Union[discord.Member, str], utils.UserTransformer(
                         sel_type=PlayerType.PLAYER, watchlist=True)]):
        if isinstance(user, discord.Member):
            ucid = await self.bot.get_ucid_by_member(user)
            if not ucid:
                # we should never be here
                # noinspection PyUnresolvedReferences
                await interaction.response.send_message(_("Member {} is not linked!").format(user.display_name))
                return
        else:
            ucid = user
        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM watchlist WHERE player_ucid = %s", (ucid, ))
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(
            _("Player {} removed from the watchlist.").format(
                user.display_name if isinstance(user, discord.Member) else user),
            ephemeral=utils.get_ephemeral(interaction))

    @watch.command(description=_('Shows the watchlist'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def list(self, interaction: discord.Interaction):
        ephemeral = utils.get_ephemeral(interaction)
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT p.ucid, p.name, w.reason, w.created_by, w.created_at 
                FROM players p JOIN watchlist w ON (p.ucid = w.player_ucid)
            """)
            watches = await cursor.fetchall()
        if not watches:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("The watchlist is currently empty."), ephemeral=ephemeral)
            return
        embed = discord.Embed(colour=discord.Colour.blue())
        embed.description = _("These players are currently on the watchlist:")
        names = created_by = ucids = ""
        for row in watches:
            names += utils.escape_string(row[1]) + '\n'
            ucids += row[0] + '\n'
            created_by += row[3] + '\n'
        embed.add_field(name=_("Name"), value=names)
        embed.add_field(name=_('UCID'), value=ucids)
        embed.add_field(name=_("Created by"), value=created_by)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(embed=embed)

    # New command group "/group"
    group = Group(name="group", description="Commands to manage DCS groups")

    @group.command(description=_('Sends a popup to a group\n'))
    @app_commands.guild_only()
    @app_commands.autocomplete(group=utils.group_autocomplete)
    @utils.app_has_roles(['DCS Admin', 'GameMaster'])
    async def popup(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                    group: str, message: str, time: Optional[Range[int, 1, 30]] = -1):
        await server.sendPopupMessage(group, message, time, interaction.user.display_name)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(_('Message sent.'), ephemeral=utils.get_ephemeral(interaction))

    @command(description=_("Links a member to a DCS user"))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def link(self, interaction: discord.Interaction, member: discord.Member,
                   user: app_commands.Transform[Union[discord.Member, str], utils.UserTransformer(
                       sel_type=PlayerType.PLAYER, linked=False)]
                   ):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        _member = DataObjectFactory().new(Member, name=member.name, node=self.node, member=member)
        if isinstance(user, discord.Member):
            _new_member = DataObjectFactory().new(Member, name=user.name, node=self.node, member=user)
            ucid = _new_member.ucid
            if ucid == _member.ucid:
                if _member.verified:
                    await interaction.followup.send(_("This member is linked to this UCID already."),
                                                    ephemeral=ephemeral)
                    return
            elif not await utils.yn_question(
                interaction, _("Member {name} is linked to another UCID ({ucid}) already. "
                               "Do you want to relink?").format(
                    name=utils.escape_string(user.display_name), ucid=ucid), ephemeral=ephemeral):
                return
            else:
                _new_member.unlink()
        else:
            ucid = user
        if _member.verified:
            if not await utils.yn_question(
                interaction, _("Member {name} is linked to another UCID ({ucid}) already. "
                               "Do you want to relink?").format(
                    name=utils.escape_string(member.display_name), ucid=_member.ucid), ephemeral=ephemeral):
                return
            else:
                _member.unlink()
        _member.link(ucid, verified=True)
        await interaction.followup.send(_('Member {name} linked to UCID {ucid}.').format(
            name=utils.escape_string(member.display_name), ucid=ucid), ephemeral=utils.get_ephemeral(interaction))
        await self.bot.audit(f'linked member {utils.escape_string(member.display_name)} to ucid {ucid}.',
                             user=interaction.user)
        # If autorole is enabled, give the user the role:
        autorole = self.bot.locals.get('autorole', {}).get('linked')
        if autorole:
            try:
                _role = self.bot.get_role(autorole)
                if not _role:
                    self.log.error(f'Role {autorole} not found!')
                    await interaction.followup.send(_("Role {} not found!").format(autorole), ephemeral=True)
                    return
                await member.add_roles(_role)
            except discord.Forbidden:
                await self.bot.audit(_('permission "Manage Roles" missing.'), user=self.bot.member)
        # Generate the onMemberLinked event
        for server_name, server in self.bot.servers.items():
            player = server.get_player(ucid=ucid, active=True)
            if player:
                player.member = self.bot.get_member_by_ucid(player.ucid)
                player.verified = True
                break
        else:
            server = None
        await self.bot.bus.send_to_node({
            "command": "rpc",
            "service": "ServiceBus",
            "method": "propagate_event",
            "params": {
                "command": "onMemberLinked",
                "server": server.name if server else None,
                "data": {
                    "ucid": ucid,
                    "discord_id": member.id
                }
            }
        })

    @command(description=_('Unlinks a member or ucid'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.describe(user=_('Name of player, member or UCID'))
    async def unlink(self, interaction: discord.Interaction,
                     user: app_commands.Transform[Union[discord.Member, str], utils.UserTransformer(linked=True)]):

        async def unlink_member(member: discord.Member, ucid: str):
            # change the link status of that member if they are an active player
            for server_name, server in self.bot.servers.items():
                player = server.get_player(ucid=ucid, active=True)
                if player:
                    player.member = None
                    player.verified = False
                    break
            else:
                await conn.execute('UPDATE players SET discord_id = -1, manual = FALSE WHERE ucid = %s', (ucid,))
                server = None
            await interaction.followup.send(_('Member {name} unlinked from UCID {ucid}.').format(
                name=utils.escape_string(member.display_name), ucid=ucid), ephemeral=ephemeral)
            await self.bot.audit(
                f'unlinked member {utils.escape_string(member.display_name)} from ucid {ucid}',
                user=interaction.user)
            await self.bot.bus.send_to_node({
                "command": "rpc",
                "service": "ServiceBus",
                "method": "propagate_event",
                "params": {
                    "command": "onMemberUnlinked",
                    "server": server.name if server else None,
                    "data": {
                        "ucid": ucid,
                        "discord_id": member.id
                    }
                }
            })

        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        async with self.apool.connection() as conn:
            async with conn.transaction():
                if isinstance(user, discord.Member):
                    member = user
                    cursor = await conn.execute('SELECT ucid FROM players WHERE discord_id = %s', (user.id, ))
                    rows = await cursor.fetchall()
                    for row in rows:
                        ucid = row[0]
                        await unlink_member(user, ucid)
                elif utils.is_ucid(user):
                    ucid = user
                    member = self.bot.get_member_by_ucid(ucid)
                    if not member:
                        await interaction.followup.send(_('Player is not linked!'), ephemeral=True)
                        return
                    await unlink_member(member, ucid)
                else:
                    await interaction.followup.send(_('Unknown player / member provided'), ephemeral=True)
                    return

        # If autorole is enabled, remove the role from the user:
        autorole = self.bot.locals.get('autorole', {}).get('linked')
        if autorole:
            try:
                await member.remove_roles(self.bot.get_role(autorole))
            except discord.Forbidden:
                await self.bot.audit(_('permission "Manage Roles" missing.'), user=self.bot.member)

    async def _find(self, interaction: discord.Interaction, name: str):
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=ephemeral)
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT distinct ucid, name, max(last_seen) FROM (
                    SELECT ucid, name, last_seen FROM players
                    UNION
                    SELECT distinct ucid, name, time AS last_seen FROM players_hist
                ) x
                WHERE x.name ILIKE %s
                GROUP BY ucid, name
                ORDER BY 3 DESC
                LIMIT 25
            """, ('%' + name + '%', ))
            rows = await cursor.fetchall()
        # give back the database session
        last_seen_str = _('last seen')
        options = [
            SelectOption(label=f"{row[1]} ({last_seen_str}: {row[2]:%Y-%m-%d %H:%M})"[:100], value=str(idx))
            for idx, row in enumerate(rows)
        ]
        if not options:
            await interaction.followup.send(_("No user found."))
            return
        idx = await utils.selection(interaction, placeholder=_("Select a User"), options=options, ephemeral=ephemeral)
        if idx:
            await self._info(interaction, rows[int(idx)][0])

    @player.command(description=_('Find a player by name'))
    @utils.app_has_role('DCS Admin')
    @app_commands.guild_only()
    async def find(self, interaction: discord.Interaction, name: str):
        await self._find(interaction, name)

    @command(description=_('Find a player by name'))
    @utils.app_has_role('DCS Admin')
    @app_commands.guild_only()
    async def find(self, interaction: discord.Interaction, name: str):
        await self._find(interaction, name)

    async def _info(self, interaction: discord.Interaction, member: Union[discord.Member, str]):
        if not member:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("This user does not exist. Try {} to find them in the historic data.").format(
                    (await utils.get_command(self.bot, name='find')).mention
                ), ephemeral=True)
            return
        ephemeral = utils.get_ephemeral(interaction)
        # noinspection PyUnresolvedReferences
        if not interaction.response.is_done():
            # noinspection PyUnresolvedReferences
            await interaction.response.defer(ephemeral=ephemeral)
        if isinstance(member, str):
            ucid = member
            member = self.bot.get_member_by_ucid(ucid)
        player: Optional[Player] = None
        for server in self.bot.servers.values():
            if isinstance(member, discord.Member):
                player = server.get_player(discord_id=member.id, active=True)
            else:
                player = server.get_player(ucid=ucid, active=True)
            if player:
                break
        else:
            server = None

        view = InfoView(member=member or ucid, bot=self.bot, ephemeral=ephemeral, player=player, server=server)
        embed = await view.render()
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
        try:
            await view.wait()
        finally:
            try:
                await msg.delete()
            except discord.NotFound:
                pass

    @player.command(description=_('Shows player information'))
    @utils.app_has_role('DCS')
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(status=[Status.RUNNING])],
                   player: app_commands.Transform[Player, utils.PlayerTransformer(active=True)]):
        report = Report(self.bot, 'mission', 'player-info.json')
        env = await report.render(player=player)
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(embed=env.embed, ephemeral=utils.get_ephemeral(interaction))

    @command(description=_('Shows player information'))
    @utils.app_has_role('DCS Admin')
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction,
                   member: app_commands.Transform[Union[discord.Member, str], utils.UserTransformer]):
        await self._info(interaction, member)

    @staticmethod
    def format_unmatched(data, marker, marker_emoji):
        embed = discord.Embed(title=_('Unlinked Players'), color=discord.Color.blue())
        embed.description = _('These players could be possibly linked:')
        ids = players = members = ''
        for i in range(0, len(data)):
            ids += (chr(0x31 + i) + '\u20E3' + '\n')
            players += "{}\n".format(utils.escape_string(data[i]['name']))
            members += f"{data[i]['match'].display_name}\n"
        embed.add_field(name=_('ID'), value=ids)
        embed.add_field(name=_('DCS Player'), value=players)
        embed.add_field(name=_('Member'), value=members)
        embed.set_footer(text=_('Press a number to link this specific user.'))
        return embed

    @command(description=_('Show players that could be linked'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def linkcheck(self, interaction: discord.Interaction):
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(thinking=True)
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                # check all unmatched players
                unmatched = []
                await cursor.execute("""
                    SELECT ucid, name FROM players 
                    WHERE discord_id = -1 AND name IS NOT NULL 
                    ORDER BY last_seen DESC
                                """)
                async for row in cursor:
                    matched_member = self.bot.match_user(dict(row), True)
                    if matched_member:
                        unmatched.append({"name": row['name'], "ucid": row['ucid'], "match": matched_member})
            if len(unmatched) == 0:
                await interaction.followup.send(_('No unmatched member could be matched.'), ephemeral=True)
                return
        n = await utils.selection_list(interaction, unmatched, self.format_unmatched)
        if n != -1:
            async with self.apool.connection() as conn:
                async with conn.transaction():
                    await conn.execute('UPDATE players SET discord_id = %s, manual = TRUE WHERE ucid = %s',
                                       (unmatched[n]['match'].id, unmatched[n]['ucid']))
                    await self.bot.audit(
                        f"linked ucid {unmatched[n]['ucid']} to user {unmatched[n]['match'].display_name}.",
                        user=interaction.user)
                    await interaction.followup.send(
                        _("DCS player {player} linked to member {member}.").format(
                            player=utils.escape_string(unmatched[n]['name']),
                            member=unmatched[n]['match'].display_name), ephemeral=True)

    @staticmethod
    def format_suspicious(data, marker, marker_emoji):
        embed = discord.Embed(title=_('Possible Mislinks'), color=discord.Color.blue())
        embed.description = _('These players could be possibly mislinked:')
        ids = players = members = ''
        for i in range(0, len(data)):
            ids += (chr(0x31 + i) + '\u20E3' + '\n')
            players += f"{data[i]['name']}\n"
            members += f"{data[i]['mismatch'].display_name}\n"
        embed.add_field(name=_('ID'), value=ids)
        embed.add_field(name=_('DCS Player'), value=players)
        embed.add_field(name=_('Member'), value=members)
        embed.set_footer(text=_('Press a number to unlink this specific user.'))
        return embed

    @command(description=_('Show possibly mislinked players'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def mislinks(self, interaction: discord.Interaction):
        # noinspection PyUnresolvedReferences
        await interaction.response.defer(thinking=True)
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                # check all matched members
                suspicious = []
                for member in self.bot.get_all_members():
                    # ignore bots
                    if member.bot:
                        continue
                    await cursor.execute("""
                        SELECT ucid, name FROM players 
                        WHERE discord_id = %s AND name IS NOT NULL AND manual = FALSE 
                        ORDER BY last_seen DESC
                    """, (member.id, ))
                    async for row in cursor:
                        matched_member = self.bot.match_user(dict(row), True)
                        if not matched_member:
                            suspicious.append({"name": row['name'], "ucid": row['ucid'], "mismatch": member})
                        elif matched_member.id != member.id:
                            suspicious.append({"name": row['name'], "ucid": row['ucid'], "mismatch": member,
                                               "match": matched_member})
                if len(suspicious) == 0:
                    await interaction.followup.send(_('No mislinked players found.'), ephemeral=True)
                    return
        n = await utils.selection_list(interaction, suspicious, self.format_suspicious)
        if n != -1:
            ephemeral = utils.get_ephemeral(interaction)
            async with self.apool.connection() as conn:
                async with conn.transaction():
                    await conn.execute('UPDATE players SET discord_id = %s, manual = %s WHERE ucid = %s',
                                       (suspicious[n]['match'].id if 'match' in suspicious[n] else -1,
                                        'match' in suspicious[n], suspicious[n]['ucid']))
                    await self.bot.audit(
                        f"unlinked ucid {suspicious[n]['ucid']} from user {suspicious[n]['mismatch'].display_name}.",
                        user=interaction.user)
                    if 'match' in suspicious[n]:
                        await self.bot.audit(
                            f"linked ucid {suspicious[n]['ucid']} to user {suspicious[n]['match'].display_name}.",
                            user=interaction.user)
                        await interaction.followup.send(
                            _("UCID {ucid} transferred from member {old_member} to member {new_member}.").format(
                                ucid=suspicious[n]['ucid'],
                                old_member=utils.escape_string(suspicious[n]['mismatch'].display_name),
                                new_member=utils.escape_string(suspicious[n]['match'].display_name)),
                            ephemeral=ephemeral)
                    else:
                        await interaction.followup.send(_("Member {name} unlinked from UCID {ucid}.").format(
                            name=utils.escape_string(suspicious[n]['mismatch'].display_name),
                            ucid=suspicious[n]['ucid']), ephemeral=ephemeral)

    @command(description=_('Link your DCS and Discord user'))
    @app_commands.guild_only()
    async def linkme(self, interaction: discord.Interaction):
        async def send_token(token: str):
            await interaction.followup.send(
                _("**Your secure TOKEN is: {token}**\n"
                  "To link your user, type in the following into the in-game chat of one of our DCS servers:"
                  "```{prefix}linkme {token}```\n\n"
                  "**The TOKEN will expire in 2 days!**").format(token=token, prefix=self.eventlistener.prefix),
                ephemeral=True)

        # noinspection PyUnresolvedReferences
        await interaction.response.defer(ephemeral=True)
        member = DataObjectFactory().new(Member, name=interaction.user.name, node=self.node, member=interaction.user)
        if member.ucid and not utils.is_ucid(member.ucid):
            await send_token(member.ucid)
            return
        if utils.is_ucid(member.ucid) and member.verified:
            if not await utils.yn_question(interaction,
                                           _("You already have a verified DCS account!\n"
                                             "Are you sure you want to re-link your account? "
                                             "(Ex: Switched from Steam to Standalone)"), ephemeral=True):
                await interaction.followup.send(_('Aborted.'))
                return
            member.unlink()

        # generate the TOKEN
        async with self.apool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    # in the unlikely event that we had a token already and a linked user
                    await cursor.execute("""
                        DELETE FROM players WHERE discord_id = %s AND length(ucid) = 4
                    """, (interaction.user.id,))
                    # in the very unlikely event that we have generated the very same random number twice
                    while True:
                        try:
                            token = str(random.randrange(1000, 9999))
                            await cursor.execute("""
                                INSERT INTO players (ucid, discord_id, last_seen) 
                                VALUES (%s, %s, NOW() AT TIME ZONE 'UTC')
                            """, (token, interaction.user.id))
                            break
                        except psycopg.errors.UniqueViolation:
                            pass
            await send_token(token)

    @player.command(description=_('Shows inactive users'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def inactive(self, interaction: discord.Interaction, period: Literal['days', 'weeks', 'months', 'years'],
                       number: Range[int, 1]):
        report = Report(self.bot, self.plugin_name, 'inactive.json')
        env = await report.render(period=f"{number} {period}")
        # noinspection PyUnresolvedReferences
        await interaction.response.send_message(embed=env.embed, ephemeral=utils.get_ephemeral(interaction))

    # New command group "/mission"
    menu = Group(name="menu", description=_("Commands to manage mission menus"))

    @menu.command(description=_('Validate the menu.yaml'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    async def validate(self, interaction: discord.Interaction):
        menu_file = os.path.join(self.node.config_dir, 'menus.yaml')
        if os.path.exists(menu_file):
            # noinspection PyUnresolvedReferences
            await interaction.response.defer(ephemeral=True)
            try:
                utils.validate(menu_file, ['schemas/menus_schema.yaml'], raise_exception=True)
                await interaction.followup.send("Schema valid.", ephemeral=True)
            except Exception as ex:
                self.log.exception(ex)
                message = traceback.format_exc()
                await interaction.followup.send(message[:2000])
        else:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(_("No menus.yaml found."), ephemeral=True)

    @tasks.loop(hours=1)
    async def expire_token(self):
        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""
                    DELETE FROM players 
                    WHERE LENGTH(ucid) = 4 AND last_seen < (DATE(now() AT TIME ZONE 'utc') - interval '2 days')
                """)

    @tasks.loop(minutes=1.0)
    async def check_for_unban(self):
        try:
            async with self.apool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute("""
                        SELECT ucid FROM bans WHERE banned_until < (NOW() AT TIME ZONE 'utc')
                    """)
                    rows = await cursor.fetchall()
                    for row in rows:
                        for server in self.bot.servers.values():
                            if server.status not in [Status.PAUSED, Status.RUNNING, Status.STOPPED]:
                                continue
                            await server.send_to_dcs({
                                "command": "unban",
                                "ucid": row[0]
                            })
                        # delete unbanned accounts from the database
                        await conn.execute("DELETE FROM bans WHERE ucid = %s", (row[0], ))
        except Exception as ex:
            self.log.exception(ex)

    @check_for_unban.before_loop
    async def before_check_unban(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5.0)
    async def update_channel_name(self):
        # might happen during a restart
        if not self.bot.member:
            return
        for server_name, server in self.bot.servers.items():
            if server.status == Status.UNREGISTERED:
                continue
            try:
                channel_id = server.channels.get(Channel.STATUS, -1)
                if channel_id == -1:
                    continue
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    channel = await self.bot.fetch_channel(server.channels[Channel.STATUS])
                # name changes of the status channel will only happen with the correct permission
                if not channel.permissions_for(self.bot.member).manage_channels:
                    return
                if channel.type == discord.ChannelType.forum:
                    continue
# TODO: Alternative implementation, if Discord decides to no longer use system messages for a thread rename
#                    for thread in channel.threads:
#                        if thread.name.startswith(server_name):
#                            channel = thread
#                            break
#                    else:
#                        continue
                name = channel.name
                if server.status in [Status.STOPPED, Status.SHUTDOWN, Status.LOADING, Status.SHUTTING_DOWN]:
                    if name.find('［') == -1:
                        name = name + '［-］'
                    else:
                        name = re.sub('［.*］', f'［-］', name)
                else:
                    players = server.get_active_players()
                    current = len(players) + 1
                    max_players = server.settings.get('maxPlayers') or 0
                    if name.find('［') == -1:
                        name = name + f'［{current}／{max_players}］'
                    else:
                        name = re.sub('［.*］', f'［{current}／{max_players}］', name)
                if name != channel.name:
                    await channel.edit(name=name)
            except Exception as ex:
                self.log.debug(f"Exception in update_channel_name() for server {server_name}", exc_info=str(ex))

    @update_channel_name.before_loop
    async def before_update_channel_name(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1.0)
    async def afk_check(self):
        try:
            for server in self.bot.servers.values():
                config = server.locals.get('afk', {})
                max_time = config.get('afk_time', -1)
                if not config or max_time == -1 or server.status != Status.RUNNING:
                    continue
                for ucid, dt in server.afk.items():
                    player = server.get_player(ucid=ucid, active=True)
                    exemptions = config.get('exemptions', {})
                    if 'discord' in exemptions:
                        exemptions['discord'] = list(set(exemptions['discord']) | {"DCS Admin", "GameMaster"})
                    if not player or player.check_exemptions(exemptions):
                        continue
                    if (datetime.now(timezone.utc) - dt).total_seconds() > max_time:
                        msg = server.locals.get('afk', {}).get(
                            'message_afk', '{player.name}, you have been kicked for being AFK for more than {time}.'
                        ).format(player=player, time=utils.format_time(max_time))
                        await server.kick(player, msg)
        except Exception as ex:
            self.log.exception(ex)

    @afk_check.before_loop
    async def before_afk_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5.0, count=2)
    async def check_roles(self):
        if not self.bot.is_ready():
            return

        role = self.bot.get_role(self.bot.locals.get('autorole', {}).get('online'))
        if role:
            online_members: set[discord.Member] = set()
            for server in self.bot.servers.values():
                for player in server.get_active_players():
                    if player.member:
                        online_members.add(player.member)
            try:
                # check who needs to lose the role
                for member in (set(role.members) - online_members):
                    await member.remove_roles(role)
            except discord.Forbidden:
                await self.bot.audit('permission "Manage Roles" missing.', user=self.bot.member)
                return
        role = self.bot.get_role(self.bot.locals.get('autorole', {}).get('linked'))
        if role:
            linked_members: set[discord.Member] = set()
            async with self.apool.connection() as conn:
                async for row in await conn.execute("""
                    SELECT DISTINCT discord_id FROM players 
                    WHERE discord_id <> -1 AND manual IS TRUE
                """):
                    member = self.bot.guilds[0].get_member(row[0])
                    if member:
                        linked_members.add(member)
            for member in (linked_members - set(role.members)):
                await member.add_roles(role)
                self.log.debug(f"=> Member {member.display_name} is linked and got the {role.name} role.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        pattern = ['.miz']
        config = self.get_config().get('uploads', {})
        if not MissionUploadHandler.is_valid(message, pattern, config.get('discord', self.bot.roles['DCS Admin'])):
            return
        # check, if upload is enabled
        if not config.get('enabled', True):
            self.log.debug("Mission upload is disabled!")
            return

        # check, if we are in the correct channel
        server = await MissionUploadHandler.get_server(message)
        if not server:
            return

        try:
            handler = MissionUploadHandler(plugin=self, server=server, message=message, pattern=pattern)
            base_dir = await handler.server.get_missions_dir()
            ignore = ['.dcssb', 'Saves', 'Scripts']
            if server.locals.get('ignore_dirs'):
                ignore.extend(server.locals['ignore_dirs'])
            await handler.upload(base_dir, ignore_list=ignore)
        except Exception as ex:
            self.log.exception(ex)
        finally:
            with suppress(discord.errors.NotFound):
                await message.delete()

    @commands.Cog.listener()
    async def on_member_ban(self, _: discord.Guild, member: discord.Member):
        self.bot.log.debug(f"Member {member.display_name} has been banned.")
        if not self.bot.locals.get('no_dcs_autoban', False):
            ucid = await self.bot.get_ucid_by_member(member)
            if ucid:
                await self.bus.ban(ucid, 'Discord',
                                   self.bot.locals.get('message_ban', 'User has been banned on Discord.'))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ucid = await self.bot.get_ucid_by_member(member, verified=True)
        autorole = self.bot.locals.get('autorole', {}).get('linked')
        if ucid and autorole:
            try:
                role = self.bot.get_role(autorole)
                await member.add_roles(role)
                self.log.debug(f"=> Rejoined member {member.display_name} got their role {role.name} back.")
            except discord.Forbidden:
                await self.bot.audit(_('permission "Manage Roles" missing.'), user=self.bot.member)


async def setup(bot: DCSServerBot):
    if 'gamemaster' not in bot.plugins:
        raise PluginRequiredError('gamemaster')
    await bot.add_cog(Mission(bot, MissionEventListener))
