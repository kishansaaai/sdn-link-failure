from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import EventMixin
import time
import heapq
try:
    import monitor_api
except ImportError:
    monitor_api = None

log = core.getLogger()

class TopologyGraph:
    def __init__(self):
        self.adj = {}  # dpid -> {dpid: port}
        self.host_locations = {}  # mac -> (dpid, port)

    def add_link(self, dpid1, port1, dpid2, port2):
        if dpid1 not in self.adj:
            self.adj[dpid1] = {}
        if dpid2 not in self.adj:
            self.adj[dpid2] = {}
        self.adj[dpid1][dpid2] = port1
        self.adj[dpid2][dpid1] = port2

    def remove_link(self, dpid1, dpid2):
        if dpid1 in self.adj and dpid2 in self.adj[dpid1]:
            del self.adj[dpid1][dpid2]
        if dpid2 in self.adj and dpid1 in self.adj[dpid2]:
            del self.adj[dpid2][dpid1]

    def dijkstra(self, src, dst):
        dist = {src: 0}
        prev = {}
        visited = set()
        heap = [(0, src)]
        
        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)
            if u == dst:
                break
            
            for v in self.adj.get(u, {}):
                nd = d + 1  # assuming edge weight of 1
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
                    
        if dst not in dist:
            return None
            
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return path

class RerouteController(EventMixin):
    def __init__(self):
        # Register for OpenFlow events (PacketIn, ConnectionUp)
        core.openflow.addListeners(self)
        # Register for Discovery events (LinkEvent)
        def startup():
            core.openflow_discovery.addListeners(self)
        core.call_delayed(0, startup) # Delay slightly to ensure discovery is loaded

        self.graph = TopologyGraph()
        self.connections = {}
        self.active_paths = {}  # (src_mac, dst_mac) -> path (list of dpids)
        self.recovery_log = []
        
        log.info("Reroute controller with topology discovery started")
        
        if monitor_api:
            monitor_api.start_api(self, port=5000)
            log.info("REST API started on port 5000")
        else:
            log.warning("monitor_api could not be imported")

    def _handle_ConnectionUp(self, event):
        self.connections[event.dpid] = event.connection
        log.info("Switch connected: %s", event.dpid)

    def _handle_LinkEvent(self, event):
        l = event.link
        if event.added:
            self.graph.add_link(l.dpid1, l.port1, l.dpid2, l.port2)
            log.info("Link UP: %s <-> %s", l.dpid1, l.dpid2)
        elif event.removed:
            t0 = time.time()
            self.graph.remove_link(l.dpid1, l.dpid2)
            log.warning("LINK FAILURE: %s <-> %s", l.dpid1, l.dpid2)
            
            # Find active paths affected by this link
            affected = []
            for key, path in self.active_paths.items():
                # Check if the failed link is in the path
                for i in range(len(path) - 1):
                    if (path[i] == l.dpid1 and path[i+1] == l.dpid2) or \
                       (path[i] == l.dpid2 and path[i+1] == l.dpid1):
                        affected.append(key)
                        break
            
            for key in affected:
                self._reroute(key, t0)

    def _reroute(self, key, t0):
        src_mac, dst_mac = key
        src_dpid = self.graph.host_locations.get(src_mac, (None, None))[0]
        dst_dpid = self.graph.host_locations.get(dst_mac, (None, None))[0]
        
        if not src_dpid or not dst_dpid:
            return
            
        new_path = self.graph.dijkstra(src_dpid, dst_dpid)
        if new_path:
            # Delete old flows on all switches first
            old_path = self.active_paths.get(key, [])
            for dpid in old_path:
                conn = self.connections.get(dpid)
                if conn:
                    msg = of.ofp_flow_mod()
                    msg.command = of.OFPFC_DELETE
                    # match src and dst mac
                    msg.match.dl_src = src_mac
                    msg.match.dl_dst = dst_mac
                    conn.send(msg)
            
            # Install new path
            self._install_path(new_path, key, src_mac, dst_mac)
            self.active_paths[key] = new_path
            
            elapsed_ms = (time.time() - t0) * 1000
            self.recovery_log.append({
                "link_pair": f"{key[0]}->{key[1]}",
                "recovery_ms": elapsed_ms,
                "new_path": new_path,
                "ts": time.time()
            })
            log.info("RECOVERY: rerouted %s->%s in %.1fms via %s", src_mac, dst_mac, elapsed_ms, new_path)
        else:
            log.error("RECOVERY FAILED: no alternate path for %s", key)

    def _install_path(self, path, key, src_mac, dst_mac):
        # We also need to know the out port for the destination host
        dst_port = self.graph.host_locations[dst_mac][1]
        
        for i in range(len(path)):
            dpid = path[i]
            
            if i < len(path) - 1:
                next_dpid = path[i+1]
                out_port = self.graph.adj[dpid][next_dpid]
            else:
                out_port = dst_port
                
            conn = self.connections.get(dpid)
            if conn:
                msg = of.ofp_flow_mod()
                msg.priority = 20
                msg.idle_timeout = 30
                msg.match.dl_src = src_mac
                msg.match.dl_dst = dst_mac
                msg.actions.append(of.ofp_action_output(port=out_port))
                conn.send(msg)

    def _handle_PacketIn(self, event):
        packet = event.parsed
        dpid = event.dpid
        in_port = event.port
        
        # Don't handle LLDP packets (discovery uses these)
        if packet.type == packet.LLDP_TYPE:
            return
            
        src_mac = packet.src
        dst_mac = packet.dst
        
        # Learn host location
        if src_mac not in self.graph.host_locations:
            # ensure we don't treat a switch-to-switch port as a host port
            # if a port is in adj, it's connected to a switch
            is_switch_port = False
            for target_dpid, port in self.graph.adj.get(dpid, {}).items():
                if port == in_port:
                    is_switch_port = True
                    break
            
            if not is_switch_port:
                self.graph.host_locations[src_mac] = (dpid, in_port)
                log.debug("Learned host %s at %s port %s", src_mac, dpid, in_port)
                
        # If we know the destination, calculate path and install rules
        if dst_mac in self.graph.host_locations:
            src_loc_dpid = self.graph.host_locations[src_mac][0]
            dst_loc_dpid = self.graph.host_locations[dst_mac][0]
            
            # calculate shortest path
            path = self.graph.dijkstra(src_loc_dpid, dst_loc_dpid)
            if path:
                key = (src_mac, dst_mac)
                self.active_paths[key] = path
                self._install_path(path, key, src_mac, dst_mac)
                
                # Also need to output this specific packet
                if len(path) > 1:
                    out_port = self.graph.adj[dpid][path[1]]
                else:
                    out_port = self.graph.host_locations[dst_mac][1]
                    
                msg = of.ofp_packet_out()
                msg.data = event.ofp
                msg.in_port = in_port
                msg.actions.append(of.ofp_action_output(port=out_port))
                event.connection.send(msg)
                return
                
        # Fallback: Flood
        msg = of.ofp_packet_out()
        msg.data = event.ofp
        msg.in_port = in_port
        msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
        event.connection.send(msg)

def launch():
    core.registerNew(RerouteController)
