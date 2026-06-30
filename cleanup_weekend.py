"""清理数据中的周末日期（周六、周日）"""
import json
import os
import re
from datetime import datetime

STATIC = os.path.join(os.path.dirname(__file__), "static")
TYPES = ["month", "60d", "120d", "1year", "alltime"]


def parse_full_label(lbl):
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", lbl or "")
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def clean_data_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    dates = data.get("dates", [])
    bad_labels = set()
    keep_idx = []
    new_dates = []
    for i, d in enumerate(dates):
        dt = parse_full_label(d.get("full_label", ""))
        if dt and dt.weekday() >= 5:
            print(f"  移除周末日期: {d['full_label']} ({os.path.basename(path)})")
            bad_labels.add(d["label"])
            continue
        keep_idx.append(i)
        new_dates.append(d)
    if not bad_labels:
        return set()
    data["dates"] = new_dates
    for row in data.get("industries", []):
        counts = row.get("daily_counts", [])
        row["daily_counts"] = [counts[i] for i in keep_idx]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return bad_labels


def clean_details_file(path, bad_labels):
    if not os.path.exists(path) or not bad_labels:
        return
    with open(path, "r", encoding="utf-8") as f:
        details = json.load(f)
    if not isinstance(details, dict):
        return
    changed = False
    for ind, dd in details.items():
        for lbl in list(dd.keys()):
            if lbl in bad_labels:
                del dd[lbl]
                changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(details, f, ensure_ascii=False)


def main():
    for direction in ("new_highs", "new_lows"):
        for t in TYPES:
            data_path = os.path.join(STATIC, f"{direction}_data_{t}.json")
            if not os.path.exists(data_path):
                continue
            bad_labels = clean_data_file(data_path)
            if bad_labels:
                details_path = os.path.join(STATIC, f"{direction}_details_{t}.json")
                clean_details_file(details_path, bad_labels)


if __name__ == "__main__":
    main()
