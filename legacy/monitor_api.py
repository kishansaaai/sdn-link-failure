from flask import Flask, jsonify
import threading

app = Flask(__name__)
controller_ref = None

@app.route('/topology')
def topology():
    if controller_ref:
        return jsonify(controller_ref.graph.adj)
    return jsonify({})

@app.route('/recovery-log')
def recovery_log():
    if controller_ref:
        return jsonify(controller_ref.recovery_log)
    return jsonify([])

def start_api(controller, port=5000):
    global controller_ref
    controller_ref = controller
    # run in thread so it doesn't block POX's main event loop
    t = threading.Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': port, 'debug': False, 'use_reloader': False})
    t.daemon = True
    t.start()
