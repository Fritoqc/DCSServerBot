from __future__ import annotations
import inspect
import json
import os
import psycopg
import shutil
import sys
import yaml
from contextlib import closing
from copy import deepcopy
from core import utils
from core.services.registry import ServiceRegistry
from discord import app_commands, Interaction
from discord.app_commands import locale_str
from discord.app_commands.commands import CommandCallback, GroupT, P, T
from discord.ext import commands, tasks
from discord.utils import MISSING, _shorten
from os import path
from pathlib import Path
from typing import Type, Optional, TYPE_CHECKING, Union, Any, Dict, Callable, List

from .listener import TEventListener

if TYPE_CHECKING:
    from core import Server
    from services import DCSServerBot, ServiceBus

DEFAULT_TAG = 'DEFAULT'


def command(
    *,
    name: Union[str, locale_str] = MISSING,
    description: Union[str, locale_str] = MISSING,
    nsfw: bool = False,
    auto_locale_strings: bool = True,
    extras: Dict[Any, Any] = MISSING,
) -> Callable[[CommandCallback[GroupT, P, T]], Command[GroupT, P, T]]:
    """Creates an application command from a regular function.

    Parameters
    ------------
    name: :class:`str`
        The name of the application command. If not given, it defaults to a lower-case
        version of the callback name.
    description: :class:`str`
        The description of the application command. This shows up in the UI to describe
        the application command. If not given, it defaults to the first line of the docstring
        of the callback shortened to 100 characters.
    nsfw: :class:`bool`
        Whether the command is NSFW and should only work in NSFW channels. Defaults to ``False``.

        Due to a Discord limitation, this does not work on subcommands.
    auto_locale_strings: :class:`bool`
        If this is set to ``True``, then all translatable strings will implicitly
        be wrapped into :class:`locale_str` rather than :class:`str`. This could
        avoid some repetition and be more ergonomic for certain defaults such
        as default command names, command descriptions, and parameter names.
        Defaults to ``True``.
    extras: :class:`dict`
        A dictionary that can be used to store extraneous data.
        The library will not touch any values or keys within this dictionary.
    """

    def decorator(func: CommandCallback[GroupT, P, T]) -> Command[GroupT, P, T]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError('command function must be a coroutine function')

        if description is MISSING:
            if func.__doc__ is None:
                desc = '…'
            else:
                desc = _shorten(func.__doc__)
        else:
            desc = description

        return Command(
            name=name if name is not MISSING else func.__name__,
            description=desc,
            callback=func,
            parent=None,
            nsfw=nsfw,
            auto_locale_strings=auto_locale_strings,
            extras=extras,
        )

    return decorator


class Command(app_commands.Command):

    def __init__(
        self,
        *,
        name: Union[str, locale_str],
        description: Union[str, locale_str],
        callback: CommandCallback[GroupT, P, T],
        nsfw: bool = False,
        parent: Optional[Group] = None,
        guild_ids: Optional[List[int]] = None,
        auto_locale_strings: bool = True,
        extras: Dict[Any, Any] = MISSING,
    ):
        super().__init__(name=name, description=description, callback=callback, nsfw=nsfw, parent=parent,
                         guild_ids=guild_ids, auto_locale_strings=auto_locale_strings, extras=extras)
        bot: DCSServerBot = ServiceRegistry.get("Bot").bot
        if 'server' in self._params and len(bot.servers) == 1:
            del self._params['server']

    async def _do_call(self, interaction: Interaction, params: Dict[str, Any]) -> T:
        if 'server' in inspect.signature(self._callback).parameters and 'server' not in params:
            params['server'] = list(interaction.client.servers.values())[0]
        return await super()._do_call(interaction=interaction, params=params)


class Group(app_commands.Group):

    def command(
        self,
        *,
        name: Union[str, locale_str] = MISSING,
        description: Union[str, locale_str] = MISSING,
        nsfw: bool = False,
        auto_locale_strings: bool = True,
        extras: Dict[Any, Any] = MISSING,
    ) -> Callable[[CommandCallback[GroupT, P, T]], Command[GroupT, P, T]]:
        """A decorator that creates an application command from a regular function under this group.

        Parameters
        ------------
        name: Union[:class:`str`, :class:`locale_str`]
            The name of the application command. If not given, it defaults to a lower-case
            version of the callback name.
        description: Union[:class:`str`, :class:`locale_str`]
            The description of the application command. This shows up in the UI to describe
            the application command. If not given, it defaults to the first line of the docstring
            of the callback shortened to 100 characters.
        nsfw: :class:`bool`
            Whether the command is NSFW and should only work in NSFW channels. Defaults to ``False``.
        auto_locale_strings: :class:`bool`
            If this is set to ``True``, then all translatable strings will implicitly
            be wrapped into :class:`locale_str` rather than :class:`str`. This could
            avoid some repetition and be more ergonomic for certain defaults such
            as default command names, command descriptions, and parameter names.
            Defaults to ``True``.
        extras: :class:`dict`
            A dictionary that can be used to store extraneous data.
            The library will not touch any values or keys within this dictionary.
        """

        def decorator(func: CommandCallback[GroupT, P, T]) -> Command[GroupT, P, T]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError('command function must be a coroutine function')

            if description is MISSING:
                if func.__doc__ is None:
                    desc = '…'
                else:
                    desc = _shorten(func.__doc__)
            else:
                desc = description

            command = Command(
                name=name if name is not MISSING else func.__name__,
                description=desc,
                callback=func,
                nsfw=nsfw,
                parent=self,
                auto_locale_strings=auto_locale_strings,
                extras=extras,
            )
            self.add_command(command)
            return command

        return decorator


class Plugin(commands.Cog):

    def __init__(self, bot: DCSServerBot, eventlistener: Type[TEventListener] = None):
        super().__init__()
        self.plugin_name = type(self).__module__.split('.')[-2]
        self.plugin_version = getattr(sys.modules['plugins.' + self.plugin_name], '__version__')
        self.bot: DCSServerBot = bot
        self.bus: ServiceBus = ServiceRegistry.get("ServiceBus")
        self.log = self.bot.log
        self.pool = self.bot.pool
        self.loop = self.bot.loop
        self.locals = self.read_locals()
        if self.plugin_name != 'commands' and 'commands' in self.locals:
            self.change_commands(self.locals['commands'], {x.name: x for x in self.get_app_commands()})
        self._config = dict[str, dict]()
        self.eventlistener: Type[TEventListener] = eventlistener(self) if eventlistener else None
        self.wait_for_on_ready.start()

    async def cog_load(self) -> None:
        await self.install()
        if self.eventlistener:
            self.bus.register_eventListener(self.eventlistener)
        self.log.info(f'  => {self.plugin_name.title()} loaded.')

    async def cog_unload(self):
        if self.eventlistener:
            await self.eventlistener.shutdown()
            self.bus.unregister_eventListener(self.eventlistener)
        # delete a possible configuration
        self._config.clear()
        self.log.info(f'  => {self.plugin_name.title()} unloaded.')

    def change_commands(self, cmds: dict, all_cmds: dict, group: app_commands.commands.Group = None) -> None:
        for name, params in cmds.items():
            cmd = all_cmds.get(name)
            if not cmd:
                self.log.warning(f"{self.plugin_name}: {name} is not a command or group!")
                continue
            if isinstance(cmd, app_commands.commands.Group):
                if 'name' in params:
                    cmd.name = params['name']
                self.change_commands(params['commands'], {x.name: x for x in cmd.commands}, cmd)
            elif isinstance(cmd, app_commands.commands.Command):
                if not params.get('enabled', True):
                    if group:
                        group.remove_command(name)
                    else:
                        self.__cog_app_commands__.remove(cmd)
                    continue
                if 'roles' in params:
                    for idx, check in enumerate(cmd.checks.copy()):
                        if 'has_role' in check.__qualname__:
                            cmd.remove_check(check)
                    if len(params['roles']):
                        cmd.add_check(utils.has_roles(params['roles'].copy()).predicate)
                if 'description' in params:
                    cmd.description = params['description']

    async def install(self):
        self.init_db()
        # create report directories for convenience
        source_path = f'./plugins/{self.plugin_name}/reports'
        if path.exists(source_path):
            target_path = f'./reports/{self.plugin_name}'
            if not path.exists(target_path):
                os.makedirs(target_path)

    def migrate(self, version: str) -> None:
        pass

    async def before_dcs_update(self) -> None:
        pass

    async def after_dcs_update(self) -> None:
        pass

    async def prune(self, conn: psycopg.Connection, *, days: int = 0, ucids: list[str] = None) -> None:
        pass

    def init_db(self) -> None:
        with self.pool.connection() as conn:
            with conn.transaction():
                with closing(conn.cursor()) as cursor:
                    cursor.execute('SELECT version FROM plugins WHERE plugin = %s', (self.plugin_name,))
                    # first installation
                    if cursor.rowcount == 0:
                        tables_file = f'./plugins/{self.plugin_name}/db/tables.sql'
                        if path.exists(tables_file):
                            with open(tables_file) as tables_sql:
                                for query in tables_sql.readlines():
                                    self.log.debug(query.rstrip())
                                    cursor.execute(query.rstrip())
                        cursor.execute('INSERT INTO plugins (plugin, version) VALUES (%s, %s) ON CONFLICT (plugin) DO '
                                       'NOTHING', (self.plugin_name, self.plugin_version))
                        self.log.info(f'  => {self.plugin_name.title()} installed.')
                    else:
                        installed = cursor.fetchone()[0]
                        # old variant, to be migrated
                        if installed.startswith('v'):
                            installed = installed[1:]
                        while installed != self.plugin_version:
                            updates_file = f'./plugins/{self.plugin_name}/db/update_v{installed}.sql'
                            if path.exists(updates_file):
                                with open(updates_file) as updates_sql:
                                    for query in updates_sql.readlines():
                                        self.log.debug(query.rstrip())
                                        cursor.execute(query.rstrip())
                            if self.plugin_version == '3.0':
                                installed = self.plugin_version
                            else:
                                ver, rev = installed.split('.')
                                installed = ver + '.' + str(int(rev) + 1)
                            self.migrate(installed)
                            self.log.info(f'  => {self.plugin_name.title()} migrated to version {installed}.')
                        cursor.execute('UPDATE plugins SET version = %s WHERE plugin = %s',
                                       (self.plugin_version, self.plugin_name))

    @staticmethod
    def migrate_to_3(plugin_name: str):
        os.makedirs('./config/backup', exist_ok=True)
        old_file = f'./config/{plugin_name}.json'
        with open(old_file, 'r') as infile:
            old = json.load(infile)
        new = {}
        for config in old['configs']:
            if 'installation' in config:
                instance = config['installation']
                new[instance] = config
                del new[instance]['installation']
            else:
                new[DEFAULT_TAG] = config
        new_file = f'./config/plugins/{plugin_name}.yaml'
        with open(new_file, 'w') as outfile:
            yaml.dump(new, outfile, default_flow_style=False)
        shutil.move(old_file, './config/backup')

    def read_locals(self) -> dict:
        if path.exists(f'./config/{self.plugin_name}.json'):
            self.log.info('  => Migrating old JSON config format to YAML ...')
            self.migrate_to_3(self.plugin_name)
            self.log.info(f'  => Config file {old_file} migrated to {new_file}.')
        if path.exists(f'./config/plugins/{self.plugin_name}.yaml'):
            filename = f'./config/plugins/{self.plugin_name}.yaml'
        elif path.exists(f'./plugins/{self.plugin_name}/config/config.yaml'):
            filename = f'./plugins/{self.plugin_name}/config/config.yaml'
        else:
            return {}
        self.log.debug(f'  => Reading plugin configuration from {filename} ...')
        return yaml.safe_load(Path(filename).read_text())

    def get_config(self, server: Optional[Server] = None, plugin_name: str = None) -> dict:
        # retrieve the config from another plugin
        if plugin_name:
            for plugin in self.bot.cogs.values():  # type: Plugin
                if plugin.plugin_name == plugin_name:
                    return plugin.get_config(server)
        if not server:
            return self.locals.get(DEFAULT_TAG, {})
        if server.instance.name not in self._config:
            self._config[server.instance.name] = \
                deepcopy(self.locals.get(DEFAULT_TAG, {}) | self.locals.get(server.instance.name, {}))
        return self._config[server.instance.name]

    def rename(self, conn: psycopg.Connection, old_name: str, new_name: str) -> None:
        # this function has to be implemented in your own plugins, if a server rename takes place
        pass

    async def on_ready(self):
        pass

    @tasks.loop(count=1, reconnect=True)
    async def wait_for_on_ready(self):
        await self.on_ready()

    @wait_for_on_ready.before_loop
    async def before_on_ready(self):
        await self.bot.wait_until_ready()


class PluginError(Exception):
    pass


class PluginRequiredError(PluginError):
    def __init__(self, plugin: str):
        super().__init__(f'Required plugin "{plugin.title()}" is missing!')


class PluginConflictError(PluginError):
    def __init__(self, plugin1: str, plugin2: str):
        super().__init__(f'Plugin "{plugin1.title()}" conflicts with plugin "{plugin2.title()}"!')


class PluginConfigurationError(PluginError):
    def __init__(self, plugin: str, option: str):
        super().__init__(f'Option "{option}" missing in {plugin}.json!')


class PluginInstallationError(PluginError):
    def __init__(self, plugin: str, reason: str):
        super().__init__(f'Plugin "{plugin.title()}" could not be installed: {reason}')
