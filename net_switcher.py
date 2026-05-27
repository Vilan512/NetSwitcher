"""
校园网助手
- 根据 config.json 按星期几设定断网/恢复时间
- 系统托盘常驻，支持手动控制和账号设置
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs
from urllib.request import Request, urlopen

import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 路径 & 常量
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
CRED_FILE = r"C:\Users\Public\Documents\user.info"
LOG_FILE = os.path.join(tempfile.gettempdir(), "net_switcher.log")

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ISP_SUFFIXES = {
    "中国移动": "@cmcc",
    "中国联通": "@unicom",
    "中国电信": "@telecom",
    "无后缀": "",
}

_log_lock = threading.Lock()
_config_lock = threading.Lock()
_net_lock = threading.Lock()  # 保护网络操作（启用/禁用/登录/登出）


# ---------------------------------------------------------------------------
# 日志（自动截断，最多保留 500 行；不记录密码）
# ---------------------------------------------------------------------------

def dbg(msg: str):
    with _log_lock:
        try:
            lines = []
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            lines.append(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}\n")
            if len(lines) > 500:
                lines = lines[-300:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "adapter": "以太网",
    "portal_login_url": "http://10.2.5.251:801/eportal/?c=Portal&a=login&login_method=1",
    "portal_logout_url": "http://10.2.5.251:801/eportal/?c=Portal&a=logout",
    "schedule": {
        "monday":    {"disable": "23:30", "enable": "07:00"},
        "tuesday":   {"disable": "23:30", "enable": "07:00"},
        "wednesday": {"disable": "23:30", "enable": "07:00"},
        "thursday":  {"disable": "23:30", "enable": "07:00"},
        "friday":    {"disable": None,    "enable": "07:00"},
        "saturday":  {"disable": None,    "enable": None},
        "sunday":    {"disable": "23:30", "enable": None},
    },
}


def load_config() -> dict:
    """读取配置文件，加锁保证线程安全。"""
    with _config_lock:
        if not os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULT_CONFIG)


def _parse_time(time_str) -> tuple[int, int] | None:
    """解析 'HH:MM'，仅接受 00:00–23:59，非法值返回 None。"""
    if not isinstance(time_str, str) or not time_str:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", time_str)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def get_today_schedule(cfg: dict) -> dict:
    day_key = DAY_KEYS[datetime.now().weekday()]
    day_cfg = cfg.get("schedule", {}).get(day_key, {})
    return {
        "disable": _parse_time(day_cfg.get("disable")),
        "enable": _parse_time(day_cfg.get("enable")),
    }


def find_next_task(cfg: dict) -> tuple[str, tuple[int, int], datetime] | None:
    """收集未来 8 天所有有效任务，按 datetime 排序返回最近的一个。"""
    now = datetime.now()
    candidates: list[tuple[str, tuple[int, int], datetime]] = []
    for offset in range(8):
        day = now + timedelta(days=offset)
        day_key = DAY_KEYS[day.weekday()]
        day_cfg = cfg.get("schedule", {}).get(day_key, {})
        for action in ("disable", "enable"):
            t = _parse_time(day_cfg.get(action))
            if t:
                target = day.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
                if target > now:
                    candidates.append((action, t, target))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[2])
    return candidates[0]


# ---------------------------------------------------------------------------
# 开机自启 (任务计划)
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "NetSwitcher"
TASK_NAME = "NetSwitcher_AutoStart"


def is_autostart_enabled() -> bool:
    r = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if r.returncode == 0:
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False


def set_autostart(enable: bool):
    # 清理旧的注册表方式
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass

    if enable:
        # 源码模式下用 python 解释器启动脚本
        if getattr(sys, "frozen", False):
            exe_path = sys.executable
            tr = f'"{exe_path}"'
        else:
            python = sys.executable
            script = os.path.abspath(__file__)
            tr = f'"{python}" "{script}"'
        r = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", TASK_NAME,
             "/TR", tr, "/SC", "ONLOGON", "/RL", "HIGHEST", "/DELAY", "0000:30"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            dbg(f"set_autostart create failed: rc={r.returncode} stderr={r.stderr.strip()}")
        else:
            dbg("set_autostart: task created")
    else:
        r = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            dbg(f"set_autostart delete failed: rc={r.returncode} stderr={r.stderr.strip()}")
        else:
            dbg("set_autostart: task deleted")


# ---------------------------------------------------------------------------
# 管理员权限
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def run_as_admin():
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# 凭据读写（用 parse_qs 解析，兼容密码含 & 或 = 的情况）
# ---------------------------------------------------------------------------

def load_credentials() -> tuple[str, str]:
    """读取凭据文件。注意：明文存储于 Public Documents，仅限本地使用。"""
    try:
        with open(CRED_FILE, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
        # 兼容旧格式 user_account=X&user_password=Y 和标准 urlencode 格式
        params = parse_qs(text, keep_blank_values=True)
        account = params.get("user_account", [""])[0]
        password = params.get("user_password", [""])[0]
        return account, password
    except (FileNotFoundError, OSError):
        return "", ""


def save_credentials(account: str, password: str):
    try:
        body = urlencode({"user_account": account, "user_password": password})
        with open(CRED_FILE, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        dbg(f"save_credentials failed: {e}")


def detect_isp(account: str) -> str:
    for name, suffix in ISP_SUFFIXES.items():
        if suffix and account.endswith(suffix):
            return name
    return "无后缀"


# ---------------------------------------------------------------------------
# 校园网登录 / 登出（统一处理 JSONP）
# ---------------------------------------------------------------------------

def _parse_portal_response(raw: str) -> dict:
    """处理 Dr.COM JSONP 响应 ({...}) → dict。"""
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return json.loads(raw)


def portal_login(account: str, password: str, url: str) -> tuple[bool, str]:
    if not account:
        return False, "未配置账号"
    try:
        body = urlencode({"user_account": account, "user_password": password})
        req = Request(url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = _parse_portal_response(raw)
        result = data.get("result")
        msg = data.get("msg", "")
        ret_code = data.get("ret_code", "")
        dbg(f"login: result={result} ret_code={ret_code}")
        if result == "1":
            return True, "登录成功"
        if ret_code == "2":
            return True, "已在线"
        return False, msg or f"登录失败 (ret_code={ret_code})"
    except Exception as e:
        dbg(f"login exception: {e}")
        return False, str(e)


def portal_logout(url: str) -> tuple[bool, str]:
    try:
        req = Request(url, method="POST")
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = _parse_portal_response(raw)
        dbg(f"logout: result={data.get('result')}")
        return data.get("result") == "1", data.get("msg", "")
    except Exception as e:
        dbg(f"logout exception: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# 网卡控制（带超时，注入防护）
# ---------------------------------------------------------------------------

def _ps_escape_param(s: str) -> str:
    """转义 PowerShell 参数中的危险字符，防止注入。"""
    return s.replace('"', '').replace("'", "").replace(";", "").replace("`", "").replace("\n", "").replace("\r", "")


def _run_ps(command: str, timeout: int = 30) -> tuple[str, str, int]:
    """执行 PowerShell 命令，返回 (stdout, stderr, returncode)。"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        dbg(f"PowerShell timeout: {command[:80]}")
        return "", "timeout", -1


def get_adapter_status(adapter: str) -> str:
    """返回 '已启用' / '已禁用' / '已断开' / '未知'。"""
    safe = _ps_escape_param(adapter)
    stdout, stderr, rc = _run_ps(
        f'Get-NetAdapter -Name "{safe}" '
        f"| Select-Object -ExpandProperty Status"
    )
    if rc != 0 or not stdout:
        if stderr:
            dbg(f"get_adapter_status failed: {stderr[:100]}")
        return "未知"
    status = stdout.strip()
    if status == "Up":
        return "已启用"
    if status in ("Disabled", "Down", "Not Present"):
        return "已禁用"
    if status == "Disconnected":
        return "已断开"
    dbg(f"get_adapter_status: unexpected status='{status}'")
    return "未知"


def disable_adapter(adapter: str):
    safe = _ps_escape_param(adapter)
    _run_ps(f'Disable-NetAdapter -Name "{safe}" -Confirm:$false')


def enable_adapter(adapter: str):
    safe = _ps_escape_param(adapter)
    _run_ps(f'Enable-NetAdapter -Name "{safe}" -Confirm:$false')


def wait_adapter_status(adapter: str, target_keyword: str, timeout: int = 15) -> bool:
    """等待网卡状态包含 target_keyword，返回是否成功。"""
    for _ in range(timeout):
        if target_keyword in get_adapter_status(adapter):
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# 设置窗口 (PowerShell WinForms)
# ---------------------------------------------------------------------------

def _ps_escape_str(s: str) -> str:
    """转义 PowerShell 双引号字符串中的特殊字符。"""
    return s.replace('"', '`"').replace('$', '`$')


def show_settings_dialog() -> bool:
    account, password = load_credentials()
    current_isp = detect_isp(account)
    pure_account = account
    for s in ISP_SUFFIXES.values():
        if s and pure_account.endswith(s):
            pure_account = pure_account[: -len(s)]
            break

    ps_path = os.path.join(tempfile.gettempdir(), "net_switcher_settings.ps1")
    try:
        ps_content = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            'Add-Type -AssemblyName System.Drawing\n'
            '$form = New-Object System.Windows.Forms.Form\n'
            '$form.Text = "校园网账号设置"\n'
            '$form.Size = New-Object System.Drawing.Size(380, 260)\n'
            '$form.StartPosition = "CenterScreen"\n'
            '$form.FormBorderStyle = "FixedDialog"\n'
            '$form.MaximizeBox = $false\n'
            '$form.TopMost = $true\n'
            '$lbl1 = New-Object System.Windows.Forms.Label\n'
            '$lbl1.Text = "校园网账号:"\n'
            '$lbl1.Location = New-Object System.Drawing.Point(20, 20)\n'
            '$lbl1.AutoSize = $true\n'
            '$form.Controls.Add($lbl1)\n'
            '$txtAccount = New-Object System.Windows.Forms.TextBox\n'
            '$txtAccount.Location = New-Object System.Drawing.Point(110, 17)\n'
            '$txtAccount.Size = New-Object System.Drawing.Size(230, 20)\n'
            f'$txtAccount.Text = "{_ps_escape_str(pure_account)}"\n'
            '$form.Controls.Add($txtAccount)\n'
            '$lbl2 = New-Object System.Windows.Forms.Label\n'
            '$lbl2.Text = "密码:"\n'
            '$lbl2.Location = New-Object System.Drawing.Point(20, 55)\n'
            '$lbl2.AutoSize = $true\n'
            '$form.Controls.Add($lbl2)\n'
            '$txtPass = New-Object System.Windows.Forms.TextBox\n'
            '$txtPass.Location = New-Object System.Drawing.Point(110, 52)\n'
            '$txtPass.Size = New-Object System.Drawing.Size(230, 20)\n'
            "$txtPass.PasswordChar = '*'\n"
            f'$txtPass.Text = "{_ps_escape_str(password)}"\n'
            '$form.Controls.Add($txtPass)\n'
            '$lbl3 = New-Object System.Windows.Forms.Label\n'
            '$lbl3.Text = "运营商:"\n'
            '$lbl3.Location = New-Object System.Drawing.Point(20, 90)\n'
            '$lbl3.AutoSize = $true\n'
            '$form.Controls.Add($lbl3)\n'
            '$cmbISP = New-Object System.Windows.Forms.ComboBox\n'
            '$cmbISP.Location = New-Object System.Drawing.Point(110, 87)\n'
            '$cmbISP.Size = New-Object System.Drawing.Size(150, 20)\n'
            '$cmbISP.DropDownStyle = "DropDownList"\n'
            '[void]$cmbISP.Items.Add("中国移动")\n'
            '[void]$cmbISP.Items.Add("中国联通")\n'
            '[void]$cmbISP.Items.Add("中国电信")\n'
            '[void]$cmbISP.Items.Add("无后缀")\n'
            f'$cmbISP.SelectedItem = "{current_isp}"\n'
            '$form.Controls.Add($cmbISP)\n'
            '$btnSave = New-Object System.Windows.Forms.Button\n'
            '$btnSave.Text = "保存"\n'
            '$btnSave.Location = New-Object System.Drawing.Point(110, 130)\n'
            '$btnSave.Size = New-Object System.Drawing.Size(140, 35)\n'
            '$btnSave.DialogResult = [System.Windows.Forms.DialogResult]::OK\n'
            '$form.AcceptButton = $btnSave\n'
            '$form.Controls.Add($btnSave)\n'
            '$result = $form.ShowDialog()\n'
            'if ($result -eq [System.Windows.Forms.DialogResult]::OK) {\n'
            '    $suffix = switch ($cmbISP.SelectedItem) {\n'
            '        "中国移动" { "@cmcc" }\n'
            '        "中国联通" { "@unicom" }\n'
            '        "中国电信" { "@telecom" }\n'
            '        default { "" }\n'
            '    }\n'
            '    $fullAccount = $txtAccount.Text.Trim() + $suffix\n'
            '    $content = "user_account=$fullAccount&user_password=$($txtPass.Text.Trim())"\n'
            f'    [System.IO.File]::WriteAllText("{CRED_FILE}", $content, [System.Text.Encoding]::UTF8)\n'
            '    [System.Windows.Forms.MessageBox]::Show("已保存: $fullAccount", "保存成功", "OK", "Information")\n'
            '    Write-Output "SAVED"\n'
            '}\n'
        )
        with open(ps_path, "w", encoding="utf-8-sig") as f:
            f.write(ps_content)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
            capture_output=True, text=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "SAVED" in result.stdout
    except Exception as e:
        dbg(f"settings dialog error: {e}")
        return False
    finally:
        try:
            os.remove(ps_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 定时调度器
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(self, on_disable, on_enable, get_config):
        self._timer: threading.Timer | None = None
        self._on_disable = on_disable
        self._on_enable = on_enable
        self._get_config = get_config

    def start(self):
        self._schedule_next()

    def cancel(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self):
        cfg = self._get_config()
        result = find_next_task(cfg)

        if not result:
            self._timer = threading.Timer(60, self._schedule_next)
            self._timer.daemon = True
            self._timer.start()
            return

        action_key, _, next_time = result
        delay = max(0.5, (next_time - datetime.now()).total_seconds())
        action = self._on_disable if action_key == "disable" else self._on_enable

        dbg(f"next: {action_key} at {next_time.strftime('%m-%d %H:%M')} (in {delay:.0f}s)")
        self._timer = threading.Timer(delay, self._run_and_reschedule, args=[action])
        self._timer.daemon = True
        self._timer.start()

    def _run_and_reschedule(self, action):
        action()
        self._schedule_next()


# ---------------------------------------------------------------------------
# 托盘图标
# ---------------------------------------------------------------------------

def make_icon_image(enabled: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (34, 197, 94) if enabled else (239, 68, 68)
    draw.ellipse([8, 8, 56, 56], fill=color)
    return img


class TrayApp:
    def __init__(self):
        self.config = load_config()
        self.scheduler = Scheduler(
            on_disable=self._do_disable,
            on_enable=self._do_enable,
            get_config=lambda: self.config,
        )
        self.icon: pystray.Icon | None = None

    @property
    def adapter(self) -> str:
        return self.config.get("adapter", "以太网")

    def run(self):
        self._startup_check()
        self.scheduler.start()
        self._create_icon()

    def _startup_check(self):
        """启动时：如果网卡禁用则先启用，然后尝试登录（已在线则无影响）。"""
        if "禁用" in get_adapter_status(self.adapter):
            now = datetime.now()
            for offset in range(1, 4):
                day = now - timedelta(days=offset)
                day_key = DAY_KEYS[day.weekday()]
                day_cfg = self.config.get("schedule", {}).get(day_key, {})
                enable_time = _parse_time(day_cfg.get("enable"))
                if enable_time:
                    h, m = enable_time
                    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if now > target:
                        dbg(f"startup: past enable time {h:02d}:{m:02d}, enabling")
                        self._do_enable()
                        return
            return
        dbg("startup: adapter enabled, trying login to refresh session")
        threading.Thread(target=self._login_only, daemon=True).start()

    # ---- 核心动作（后台线程，加 _net_lock 防并发）------------------------

    def _do_disable(self):
        with _net_lock:
            dbg("do_disable: logout + disable adapter")
            portal_logout(self.config["portal_logout_url"])
            disable_adapter(self.adapter)
            wait_adapter_status(self.adapter, "未知")  # Disabled → "未知" 或 "已禁用"
            self._refresh()

    def _do_enable(self):
        with _net_lock:
            dbg("do_enable: enable adapter + login")
            enable_adapter(self.adapter)
            # 等待网卡 Up（可能需要较长时间，尤其从 Disconnected 恢复）
            if not wait_adapter_status(self.adapter, "已启用", timeout=30):
                dbg("do_enable: adapter not ready after 30s, trying login anyway")
            # 登录重试（最多 3 次，间隔 5 秒）
            for attempt in range(3):
                account, password = load_credentials()
                ok, msg = portal_login(account, password, self.config["portal_login_url"])
                dbg(f"do_enable: login attempt={attempt+1} ok={ok} msg={msg}")
                if ok:
                    break
                if attempt < 2:
                    time.sleep(5)
            self._refresh()

    def _login_only(self):
        """只登录，不操作网卡（用于睡眠恢复等场景）。"""
        with _net_lock:
            self._login_only_inner()

    def _login_only_inner(self):
        """内部调用，假设已持有 _net_lock。"""
        account, password = load_credentials()
        ok, msg = portal_login(account, password, self.config["portal_login_url"])
        dbg(f"login: ok={ok} msg={msg}")
        self._refresh()

    # ---- 托盘 -----------------------------------------------------------

    def _create_icon(self):
        enabled = "启用" in get_adapter_status(self.adapter)
        self.icon = pystray.Icon(
            "net_switcher",
            make_icon_image(enabled),
            "校园网助手",
            menu=self._build_menu(),
        )
        self.icon.run()

    def _build_menu(self) -> pystray.Menu:
        adapter = self.adapter
        status_text = f"{adapter}状态: {get_adapter_status(adapter)}"
        sched = get_today_schedule(self.config)
        disable_str = f"{sched['disable'][0]:02d}:{sched['disable'][1]:02d}" if sched["disable"] else "无"
        enable_str = f"{sched['enable'][0]:02d}:{sched['enable'][1]:02d}" if sched["enable"] else "无"
        today_label = DAY_LABELS[datetime.now().weekday()]

        return pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.MenuItem(
                f"今日({today_label}) 断网:{disable_str} 恢复:{enable_str}",
                None, enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("手动禁用以太网 (断网+登出)", self._on_disable),
            pystray.MenuItem("手动启用以太网 (联网+登录)", self._on_enable),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("账号设置", self._on_settings),
            pystray.MenuItem("开机自启动", self._on_toggle_autostart, checked=lambda *_: is_autostart_enabled()),
            pystray.MenuItem("重新加载配置", self._on_reload),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._on_quit),
        )

    def _refresh(self):
        if not self.icon:
            return
        enabled = "启用" in get_adapter_status(self.adapter)
        self.icon.icon = make_icon_image(enabled)
        self.icon.menu = self._build_menu()

    def _on_disable(self, *_):
        threading.Thread(target=self._do_disable, daemon=True).start()

    def _on_enable(self, *_):
        threading.Thread(target=self._do_enable, daemon=True).start()

    def _on_settings(self, *_):
        def _do():
            if show_settings_dialog():
                self._do_enable()
        threading.Thread(target=_do, daemon=True).start()

    def _on_toggle_autostart(self, *_):
        new_state = not is_autostart_enabled()
        set_autostart(new_state)

    def _on_reload(self, *_):
        # load_config() 内部已加锁，此处不要再包 _config_lock
        self.config = load_config()
        self.scheduler.cancel()
        self.scheduler.start()
        self._refresh()

    def _on_quit(self, *_):
        self.scheduler.cancel()
        self.icon.stop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    if not is_admin():
        run_as_admin()
    dbg("=== NetSwitcher started ===")
    TrayApp().run()


if __name__ == "__main__":
    main()
