"""Сколько стоит работа без предохранителя. Измерение, а не рассуждение.

Считаем на ценах из ЖИВОГО каталога платформы (`_caps_live.json`, снят
открытой тулзой list_capabilities) и на локальном двойнике, который повторяет
поведение боевого API: без strict лишние поля молча отбрасываются, а деньги
списываются.

Три замера:
  1. Партия запросов с типичными ошибками: сколько списывается по-наивному и
     сколько с клиентом. Разница это спасённые рубли.
  2. Цена одной ошибки поля по каждой видео-модели, по их прайсу.
  3. Скорость цикла отладки: двойник против боевого API.

    python bench.py

Честная граница: доля ошибок в партии задана сценарием (он описан ниже), а не
снята с живых пользователей. Проверяется воспроизводимо: те же запросы,
одни и те же цены, два способа отправки.
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vibe_fake                                   # noqa: E402
from vibe import Vibe, VibeError                   # noqa: E402

CAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_caps_live.json")

# Сценарий партии: 25 запросов, из них 6 с ошибками, которые документация сама
# называет частыми. Это 24 процента, доля выбрана мной, не измерена на людях.
BATCH = (
    [{"model": "z-image", "type": "image", "fields": {}}] * 10 +
    [{"model": "grok-itv", "type": "video",
      "fields": {"image": "https://example.com/cat.png"}}] * 5 +
    [{"model": "seedream-5-pro-edit", "type": "image",
      "fields": {"image": "https://example.com/a.png"}}] * 4 +
    # ошибка 1: поле картинки от image-модели отправлено видео-модели
    [{"model": "grok-itv", "type": "video",
      "fields": {"image_input": "https://example.com/cat.png"}, "bad": "поле image_input у видео"}] * 3 +
    # ошибка 2: опечатка в названии параметра
    [{"model": "z-image", "type": "image",
      "fields": {"aspekt_ratio": "16:9"}, "bad": "опечатка в имени параметра"}] * 2 +
    # ошибка 3: имя модели из документации, которого нет в каталоге
    [{"model": "grok-itv-10", "type": "video",
      "fields": {"image": "https://example.com/cat.png"}, "bad": "имя модели из документации"}]
)


def load_live_prices():
    """Подменяем прайс двойника на настоящий, чтобы считать в реальных рублях."""
    if not os.path.exists(CAPS):
        print("нет _caps_live.json, сначала: python check_live.py")
        sys.exit(1)
    caps = json.load(open(CAPS, encoding="utf-8"))
    models = []
    for kind, node in caps.get("models", {}).items():
        for name, spec in node.items():
            price = spec.get("price")
            if not isinstance(price, (int, float)):
                continue
            params = list(spec.get("required", [])) + list(spec.get("optional", []))
            models.append({"model": name, "type": kind, "params": params, "price": float(price)})
    vibe_fake.MODELS = models
    vibe_fake.BY_NAME = {m["model"]: m for m in models}
    vibe_fake.CAPS = {"models": {}}
    for m in models:
        vibe_fake.CAPS["models"].setdefault(m["type"], {})[m["model"]] = {
            "price": m["price"], "required": ["prompt"],
            "optional": [p for p in m["params"] if p != "prompt"]}
    return models


def run_batch(v, naive):
    """naive=True повторяет примеры из документации: без strict, поле кладётся
    руками. naive=False идёт через клиент."""
    spent_start = v.balance()["balance"]
    sent = errors = 0
    for item in BATCH:
        try:
            if naive:
                body = {"type": item["type"], "model": item["model"], "prompt": "замер"}
                for k, val in item["fields"].items():
                    body[k if k != "image" else "image_urls"] = (
                        [val] if k == "image" else val)
                v._call("POST", "/generate", body)     # без strict, как в примерах
            else:
                v.generate(item["type"], item["model"], "замер", **item["fields"])
            sent += 1
        except VibeError:
            errors += 1
    return round(spent_start - v.balance()["balance"], 2), sent, errors


def bench_batch():
    print("ЗАМЕР 1. Партия из", len(BATCH), "запросов, из них с ошибками:",
          sum(1 for x in BATCH if x.get("bad")))
    rows = []
    for naive in (True, False):
        srv, base = vibe_fake.serve(port=0, balance=100000)
        try:
            v = Vibe("bench", base=base)
            spent, sent, errs = run_batch(v, naive)
            rows.append((spent, sent, errs))
        finally:
            srv.shutdown()
    naive_spent, naive_sent, _ = rows[0]
    safe_spent, safe_sent, safe_errs = rows[1]
    print(f"  как в примерах документации: списано {naive_spent} р, ушло запросов {naive_sent}")
    print(f"  через клиент:                списано {safe_spent} р, ушло {safe_sent}, "
          f"отбито до отправки {safe_errs}")
    print(f"  СПАСЕНО: {round(naive_spent - safe_spent, 2)} р на партии из {len(BATCH)} запросов")
    share = (naive_spent - safe_spent) / naive_spent * 100 if naive_spent else 0
    print(f"  это {share:.1f} процента бюджета партии\n")


def bench_field_cost(models):
    print("ЗАМЕР 2. Цена одной ошибки поля, по вашему прайсу")
    video = sorted([m for m in models if m["type"] == "video"],
                   key=lambda m: -m["price"])[:8]
    for m in video:
        print(f"  {m['model']:<24} {m['price']:>8.2f} р уходит впустую")
    if video:
        avg = sum(m["price"] for m in video) / len(video)
        print(f"  в среднем по восьми моделям: {avg:.2f} р за один промах\n")


def bench_debug_cycle():
    """Цикл отладки: двойник против боевых таймингов из документации."""
    print("ЗАМЕР 3. Скорость цикла отладки")
    srv, base = vibe_fake.serve(port=0, balance=100000)
    try:
        v = Vibe("bench", base=base)
        t0 = time.time()
        n = 20
        for _ in range(n):
            g = v.generate("image", "z-image", "цикл отладки")
            v.wait(g, timeout=30, poll=0.2)
        local = time.time() - t0
    finally:
        srv.shutdown()
    # из документации: image 30-90 секунд, берём нижнюю границу, чтобы не завышать
    real = n * 30
    print(f"  {n} итераций на двойнике: {local:.1f} с")
    print(f"  те же {n} итераций на боевом API: минимум {real} с по вашим же срокам "
          f"(image 30-90 с)")
    print(f"  разница: в {real / local:.0f} раз быстрее и за 0 рублей вместо реальных списаний\n")


if __name__ == "__main__":
    models = load_live_prices()
    print(f"прайс взят из живого каталога: моделей с ценой {len(models)}\n")
    bench_batch()
    bench_field_cost(models)
    bench_debug_cycle()
