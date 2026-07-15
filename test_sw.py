from mininet.node import OVSSwitch
import inspect

print(inspect.getsource(OVSSwitch.__init__))
print(inspect.getsource(OVSSwitch.start))
