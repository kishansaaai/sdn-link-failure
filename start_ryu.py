import eventlet
eventlet.monkey_patch()
import sys
from ryu.cmd.manager import main
if __name__ == '__main__':
    sys.argv = ['ryu-manager', '--observe-links', 'ryu_controller.sdn_controller']
    sys.exit(main())
