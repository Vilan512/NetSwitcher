"""
NetSwitcher 自检脚本
覆盖：_parse_time、find_next_task、凭据读写、_ps_escape_param
"""

import sys
import os
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from net_switcher import _parse_time, find_next_task, load_credentials, save_credentials, _ps_escape_param
from datetime import datetime, timedelta


def test_parse_time():
    print("=== _parse_time ===")
    cases = [
        ("07:00", (7, 0)),
        ("23:30", (23, 30)),
        ("00:00", (0, 0)),
        ("23:59", (23, 59)),
        ("9:05", (9, 5)),
        # 非法值
        ("24:00", None),
        ("12", None),
        ("aa:bb", None),
        ("99:99", None),
        ("", None),
        (None, None),
        (123, None),
        ("-1:00", None),
        ("12:60", None),
    ]
    passed = 0
    for input_val, expected in cases:
        result = _parse_time(input_val)
        ok = result == expected
        if not ok:
            print(f"  FAIL: _parse_time({input_val!r}) = {result}, expected {expected}")
        passed += ok
    print(f"  {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_find_next_task():
    print("=== find_next_task ===")
    cfg = {
        "schedule": {
            "monday":    {"disable": "23:30", "enable": "07:00"},
            "tuesday":   {"disable": "23:30", "enable": "07:00"},
            "wednesday": {"disable": "23:30", "enable": "07:00"},
            "thursday":  {"disable": "23:30", "enable": "07:00"},
            "friday":    {"disable": None,    "enable": "07:00"},
            "saturday":  {"disable": None,    "enable": None},
            "sunday":    {"disable": "23:30", "enable": None},
        }
    }
    import net_switcher as ns

    # 测试 1: 周一 06:30 → 应返回当天 07:00 enable
    fake = datetime(2026, 5, 25, 6, 30, 0)
    with patch.object(ns, "datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = fake
        result = find_next_task(cfg)
    assert result is not None, "Mon 06:30 returned None"
    action, t, target = result
    ok1 = action == "enable" and t == (7, 0)
    print(f"  Mon 06:30 -> {action} {t} {'OK' if ok1 else 'FAIL'}")

    # 测试 2: 周一 08:00 → 应返回当天 23:30 disable
    fake = datetime(2026, 5, 25, 8, 0, 0)
    with patch.object(ns, "datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = fake
        result = find_next_task(cfg)
    assert result is not None
    action, t, target = result
    ok2 = action == "disable" and t == (23, 30)
    print(f"  Mon 08:00 -> {action} {t} {'OK' if ok2 else 'FAIL'}")

    # 测试 3: 周六 10:00 → 应返回周日 23:30 disable（跨天）
    fake = datetime(2026, 5, 30, 10, 0, 0)  # 周六
    with patch.object(ns, "datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = fake
        result = find_next_task(cfg)
    assert result is not None, "Sat 10:00 returned None"
    action, t, target = result
    ok3 = action == "disable" and t == (23, 30) and target.weekday() == 6
    print(f"  Sat 10:00 -> {action} {t} (Sun) {'OK' if ok3 else 'FAIL'}")

    # 测试 4: 非法时间应被跳过
    cfg_bad = {
        "schedule": {
            "monday": {"disable": "99:99", "enable": "07:00"},
        }
    }
    fake = datetime(2026, 5, 25, 6, 0, 0)
    with patch.object(ns, "datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = fake
        result = find_next_task(cfg_bad)
    assert result is not None, "Bad time should skip"
    action, t, target = result
    ok4 = action == "enable" and t == (7, 0)
    print(f"  Bad disable -> skips to enable {t} {'OK' if ok4 else 'FAIL'}")

    return all([ok1, ok2, ok3, ok4])


def test_credentials():
    print("=== credentials read/write ===")
    test_cred = os.path.join(tempfile.gettempdir(), "test_cred.info")
    import net_switcher
    orig_cred = net_switcher.CRED_FILE
    net_switcher.CRED_FILE = test_cred

    # 测试 1: 普通账号
    save_credentials("12233993@telecom", "Wys.0929")
    a, p = load_credentials()
    ok1 = (a, p) == ("12233993@telecom", "Wys.0929")
    print(f"  Normal: {a} {'OK' if ok1 else 'FAIL'}")

    # 测试 2: 密码含 & 和 =
    save_credentials("test@cmcc", "p@ss&w0rd=123")
    a, p = load_credentials()
    ok2 = (a, p) == ("test@cmcc", "p@ss&w0rd=123")
    print(f"  Special chars: {a} / {p} {'OK' if ok2 else 'FAIL'}")

    # 测试 3: 空账号
    save_credentials("", "")
    a, p = load_credentials()
    ok3 = (a, p) == ("", "")
    print(f"  Empty: '{a}' / '{p}' {'OK' if ok3 else 'FAIL'}")

    net_switcher.CRED_FILE = orig_cred
    try:
        os.remove(test_cred)
    except OSError:
        pass
    return all([ok1, ok2, ok3])


def test_ps_escape():
    print("=== _ps_escape_param ===")
    cases = [
        ("以太网", "以太网"),
        ('a"b;c', "abc"),
        ("test`injection\n", "testinjection"),
        ("normal", "normal"),
    ]
    passed = 0
    for input_val, expected in cases:
        result = _ps_escape_param(input_val)
        ok = result == expected
        if not ok:
            print(f"  FAIL: _ps_escape_param({input_val!r}) = {result!r}, expected {expected!r}")
        passed += ok
    print(f"  {passed}/{len(cases)} passed")
    return passed == len(cases)


if __name__ == "__main__":
    results = [
        test_parse_time(),
        test_find_next_task(),
        test_credentials(),
        test_ps_escape(),
    ]
    total = sum(results)
    print(f"\n{'='*40}")
    print(f"Result: {total}/{len(results)} test groups passed")
    if all(results):
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
