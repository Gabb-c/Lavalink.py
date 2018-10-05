import asyncio
import logging
import copy

from .PlayerManager import PlayerManager
from .Events import NodeReadyEvent, NodeDisabledEvent
from .Stats import Stats
from .WebSocket import WebSocket

log = logging.getLogger(__name__)
DISCORD_REGIONS = ["amsterdam", "brazil", "eu-central", "eu-west", "frankfurt", "hongkong", "japan", "london", "russia",
                   "singapore", "southafrica", "sydney", "us-central", "us-east", "us-south", "us-west",
                   "vip-amsterdam", "vip-us-east", "vip-us-west"]


class RegionNotFound(Exception):
    pass


class NoNodesAvailable(Exception):
    pass


class Regions:
    def __init__(self, region_list: list = None):
        self.regions = region_list or DISCORD_REGIONS
        for r in self.regions:
            if r not in DISCORD_REGIONS:
                raise RegionNotFound("Invalid region: {}".format(r))

    def __iter__(self):
        for r in self.regions:
            yield r

    @classmethod
    def all(cls):
        return cls()

    @classmethod
    def eu(cls):
        """ All servers in Europe including Russia, because majority of population is closer to EU. """
        return cls(["amsterdam", "eu-central", "eu-west", "frankfurt", "london", "russia", "vip-amsterdam"])

    @classmethod
    def us(cls):
        """ All servers located in United States """
        return cls(["us-central", "us-east", "us-south", "us-west", "vip-us-east", "vip-us-west"])

    @classmethod
    def america(cls):
        """ All servers in North and South America. """
        return cls(["us-central", "us-east", "us-south", "us-west", "vip-us-east", "vip-us-west", "brazil"])

    @classmethod
    def africa(cls):
        """ All servers in Africa. """
        return cls(["southafrica"])

    @classmethod
    def asia(cls):
        """ All servers located in Asia """
        return cls(["hongkong", "japan", "singapore"])

    @classmethod
    def oceania(cls):
        """ All servers located in Australia """
        return cls(["sydney"])

    @classmethod
    def half_one(cls):
        """ EU, Africa, Brazil and East US """
        return cls(["amsterdam", "brazil", "eu-central", "eu-west", "frankfurt", "london", "southafrica",
                    "us-east", "vip-amsterdam", "vip-us-east"])

    @classmethod
    def half_two(cls):
        """ West US, Asia and Oceania """
        return cls(["hongkong", "japan", "russia", "singapore", "sydney", "us-central", "us-south", "us-west",
                    "vip-us-west"])

    @classmethod
    def third_one(cls):
        """ EU, Russia and Africa """
        return cls(["amsterdam", "eu-central", "eu-west", "frankfurt", "london", "russia", "southafrica",
                    "vip-amsterdam"])

    @classmethod
    def third_two(cls):
        """ Asia and Oceania """
        return cls(["hongkong", "japan", "singapore", "sydney"])

    @classmethod
    def third_three(cls):
        """ North and South America """
        return cls(["us-central", "us-east", "us-south", "us-west", "vip-us-east", "vip-us-west"])


class LavalinkNode:
    def __init__(self, manager, host, password, regions, rest_port: int = 2333, ws_port: int = 80,
                 ws_retry: int = 10, shard_count: int = 1):
        self.regions = regions
        self._lavalink = manager._lavalink
        self.manager = manager
        self.rest_uri = 'http://{}:{}/loadtracks?identifier='.format(host, rest_port)
        self.password = password

        self.ws = WebSocket(
            manager._lavalink, self, host, password, ws_port, ws_retry, shard_count
        )
        self.server_version = 2
        self.stats = Stats()

        self.ready = asyncio.Event(loop=self._lavalink.loop)

        self.players = PlayerManager(self._lavalink, self, self.manager.default_player)

    def set_online(self):
        self.manager.on_node_ready(self)

    def set_offline(self):
        self.manager.on_node_disabled(self)

    async def manage_failover(self):
        if self.manager.nodes:
            new_node = self.manager.nodes[0]
            for g in list(self.players._players):
                new_player = self.players._players.pop(g)
                new_player.node = new_node
                is_playing = bool(new_player.is_playing)
                current_track = copy.copy(new_player.previous)
                current_posit = copy.copy(new_player._prev_position)
                new_node.players._players.update({g: new_player})
                ws = self._lavalink.bot._connection._get_websocket(int(g))
                await ws.voice_state(int(g), None)
                if is_playing:
                    await new_player.connect(new_player.channel_id)
                    new_player.queue.insert(0, current_track)
                    await new_player.play()
                    await new_player.seek(current_posit)


class NodeManager:
    def __init__(self, lavalink, default_node, round_robin, player):
        self._lavalink = lavalink  # lavalink client
        self.default_player = player

        self.default_node_index = default_node  # index of the default node for REST
        self.round_robin = round_robin  # enable round robin load balancing
        self._rr_pos = 0  # starting sound robin position

        self.nodes = []  # list of nodes (online)
        self.offline_nodes = []  # list of nodes (offline or not set-up yet)
        self.nodes_by_region = {}  # dictionary of nodes with region keys

        self._hooks = []

        self.ready = asyncio.Event(loop=self._lavalink.loop)

    def __iter__(self):
        for node in self.nodes:
            yield node

    async def _dispatch_node_event(self, event):
        """ Dispatches a node event to all registered hooks. """
        log.debug('Dispatching event of type {} to {} hooks'.format(event.__class__.__name__, len(self._hooks)))
        for hook in self._hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook(event)
                else:
                    hook(event)
            except Exception as e:  # pylint: disable=broad-except
                # Catch generic exception thrown by user hooks
                log.warning(
                    'Encountered exception while dispatching an event to hook `{}` ({})'.format(hook.__name__, str(e)))

    def register_node_hook(self, func):
        """ Register a hook for receiving node updates. """
        if func not in self._hooks:
            self._hooks.append(func)

    def unregister_node_hook(self, func):
        """ Unregister a hook for receiving node updated. """
        if func in self._hooks:
            self._hooks.remove(func)

    def on_node_ready(self, node):
        if node not in self.offline_nodes:
            return
        node_index = self.offline_nodes.index(node)
        self.nodes.append(self.offline_nodes.pop(node_index))
        log.info("Node {} is ready for use.".format(self.nodes.index(node)))
        node.ready.set()
        self.ready.set()
        for region in node.regions:
            self.nodes_by_region.update({region: node})
        self._lavalink.loop.create_task(self._dispatch_node_event(NodeReadyEvent(node)))

    def on_node_disabled(self, node):
        if node not in self.nodes:
            return
        node_index = self.nodes.index(node)
        self.offline_nodes.append(self.nodes.pop(node_index))
        node.ready.clear()
        if not self.nodes:
            self.ready.clear()
        log.info("Node {} was removed from use.".format(node_index))
        if not self.nodes:
            log.warning("Node {} is offline and it's the only node in the cluster.".format(node_index))
            return
        default_node = self.nodes[0]
        for region in node.regions:
            self.nodes_by_region.update({region: default_node})
        self._lavalink.loop.create_task(self._dispatch_node_event(NodeDisabledEvent(node)))

    def add(self, regions: Regions, host='localhost', rest_port=2333, password='', ws_retry=10, ws_port=80,
            shard_count=1):
        node = LavalinkNode(self, host, password, regions, rest_port, ws_port, ws_retry, shard_count)
        self.offline_nodes.append(node)

    def get_rest(self):
        if not self.nodes:
            raise NoNodesAvailable
        node = self.nodes[self.default_node_index] if self.default_node_index < len(self.nodes) else None
        if node is None and self.round_robin is False:
            node = self.nodes[0]
        if self.round_robin:
            node = self.nodes[min(self._rr_pos, len(self.nodes) - 1)]
            self._rr_pos += 1
            if self._rr_pos > len(self.nodes):
                self._rr_pos = 0
        return node

    def get_by_region(self, guild):
        node = self.nodes_by_region.get(str(guild.region), None)
        if node is None:
            log.info("Unknown region: {}".format(str(guild.region)))
            node = self.nodes[0]
        return node.players.get(guild.id)