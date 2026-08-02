import os, sys, json
from datetime import datetime
sys.path.insert(0, r"C:\Users\Administrator\Desktop\Projects\outreach")
sys.stdout.reconfigure(encoding="utf-8")
import ops

ops.STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_state.json")
py = sys.executable
ops.SOURCES = {
    "telegram": ([py, "-c", "print('[лс] Иван | непрочитанных 2'); print('шум')"], 30),
    "hh":       ([py, "-c", "import sys; sys.exit(3)"], 30),
    "почта":    ([py, "-c", "import time; time.sleep(30)"], 2),
    "kwork":    ([py, "kw_snap.py"], 300),
}
now = datetime.now()
s = ops.load(now)
ops.collect(s, now)
print()
print(ops.table(s, now))
print("\n".join(ops.details(s)))
p = s["площадки"]
assert p["telegram"]["ждут"] == ["[лс] Иван | непрочитанных 2"], p["telegram"]
assert p["telegram"]["проверок"] == 1 and p["telegram"]["ошибка"] is None
assert "код 3" in p["hh"]["ошибка"] and p["hh"]["проверок"] == 0
assert "Timeout" in p["почта"]["ошибка"] and p["почта"]["проверок"] == 0
assert json.load(open(ops.STATE, encoding="utf-8"))["площадки"]["telegram"]["ждут"], "состояние не записалось"
print("\nok: падение и таймаут источника не уронили сбор, состояние на диске")
