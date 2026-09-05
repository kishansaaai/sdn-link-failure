"""Compatibility module for the supported OpenFlow 1.3 application.

Start using python start_ryu.py. The historical POX application is in legacy/.
"""
from ryu_controller.sdn_controller import SDNController

__all__ = ["SDNController"]
