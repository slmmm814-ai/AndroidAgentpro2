import json, time, os

CMD_FILE = "/sdcard/agent_command.json"
RES_FILE = "/sdcard/agent_result.json"

def send_command(cmd, timeout=10):
    if os.path.exists(RES_FILE):
        os.remove(RES_FILE)
    with open(CMD_FILE, "w") as f:
        json.dump(cmd, f)
    
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(RES_FILE):
            with open(RES_FILE, "r") as f:
                return json.load(f).get("result")
        time.sleep(0.3)
    return "timeout"

def click(x, y):
    return send_command({"action": "click", "x": x, "y": y})

def status():
    return send_command({"action": "status"})
