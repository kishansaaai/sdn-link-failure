from pox.core import core
from pox.lib.util import dpidToStr
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import *

log = core.getLogger()

class LinkFailureController(EventMixin):
    def __init__(self):
        self.listenTo(core.openflow)
        self.mac_to_port = {}
        log.info("Link Failure Detection and Recovery Controller Started")

    def _handle_ConnectionUp(self, event):
        self.mac_to_port[event.dpid] = {}
        log.info("Switch %s connected", dpidToStr(event.dpid))

    def _handle_PacketIn(self, event):
        packet = event.parsed
        dpid = event.dpid
        in_port = event.port
        if dpid not in self.mac_to_port:
            self.mac_to_port[dpid] = {}
        self.mac_to_port[dpid][packet.src] = in_port
        if packet.dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][packet.dst]
            msg = of.ofp_flow_mod()
            msg.match = of.ofp_match.from_packet(packet, in_port)
            msg.idle_timeout = 0
            msg.hard_timeout = 0
            msg.priority = 10
            msg.data = event.ofp
            msg.actions.append(of.ofp_action_output(port=out_port))
            event.connection.send(msg)
        else:
            msg = of.ofp_packet_out()
            msg.data = event.ofp
            msg.in_port = in_port
            msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
            event.connection.send(msg)

    def _handle_PortStatus(self, event):
        dpid = event.dpid
        port = event.ofp.desc.port_no
        if event.ofp.reason == of.OFPPR_DELETE:
            log.warning("LINK FAILURE DETECTED: Switch %s Port %s DOWN", dpidToStr(dpid), port)
            if dpid in self.mac_to_port:
                self.mac_to_port[dpid] = {}
            msg = of.ofp_flow_mod()
            msg.command = of.OFPFC_DELETE
            event.connection.send(msg)
            log.info("RECOVERY: Flow tables cleared, re-learning paths")
        elif event.ofp.reason == of.OFPPR_MODIFY:
            log.info("Port RESTORED: Switch %s Port %s UP", dpidToStr(dpid), port)

def launch():
    core.registerNew(LinkFailureController)
    log.info("Link Failure Detection and Recovery module loaded.")
