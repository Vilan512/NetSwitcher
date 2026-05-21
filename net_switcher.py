"""
校园网助手
- 根据 config.json 按星期几设定断网/恢复时间
- 系统托盘常驻，支持手动控制和账号设置
"""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import winreg
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
CRED_FILE = r"C:\Users\Public\Documents\user.info"

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ISP_SUFFIXES = {
    "中国移动": "@cmcc",
    "中国联通": "@unicom",
    "中国电信": "@telecom",
    "无后缀": "",
}

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
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_time(time_str: str) -> tuple[int, int] | None:
    """解析 'HH:MM' 字符串，返回 (hour, minute)。"""
    if not time_str:
        return None
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def get_today_schedule(cfg: dict) -> dict:
    """返回今天的 {'disable': (h,m)|None, 'enable': (h,m)|None}。"""
    day_key = DAY_KEYS[datetime.now().weekday()]
    day_cfg = cfg.get("schedule", {}).get(day_key, {})
    return {
        "disable": _parse_time(day_cfg.get("disable")),
        "enable": _parse_time(day_cfg.get("enable")),
    }


# ---------------------------------------------------------------------------
# 开机自启 (注册表)
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "NetSwitcher"


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False


def set_autostart(enable: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


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
# 凭据读写
# ---------------------------------------------------------------------------

def load_credentials() -> tuple[str, str]:
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        account, password = "", ""
        for part in text.split("&"):
            if part.startswith("user_account="):
                account = part.split("=", 1)[1]
            elif part.startswith("user_password="):
                password = part.split("=", 1)[1]
        return account, password
    except FileNotFoundError:
        return "", ""


def save_credentials(account: str, password: str):
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        f.write(f"user_account={account}&user_password={password}")


def detect_isp(account: str) -> str:
    for name, suffix in ISP_SUFFIXES.items():
        if suffix and account.endswith(suffix):
            return name
    return "无后缀"


# ---------------------------------------------------------------------------
# 校园网登录 / 登出
# ---------------------------------------------------------------------------

def portal_login(account: str, password: str, url: str) -> tuple[bool, str]:
    if not account:
        return False, "未配置账号"
    try:
        body = urlencode({"user_account": account, "user_password": password})
        req = Request(url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("result")
        msg = data.get("msg", "")
        ret_code = data.get("ret_code", "")
        if result == "1":
            return True, "登录成功"
        if ret_code == "2":
            return True, "已在线"
        return False, msg or f"登录失败 (ret_code={ret_code})"
    except Exception as e:
        return False, str(e)


def portal_logout(url: str) -> tuple[bool, str]:
    try:
        req = Request(url, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("result") == "1", data.get("msg", "")
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 网卡控制
# ---------------------------------------------------------------------------

def _run_ps(command: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result.stdout.strip()


def get_adapter_status(adapter: str) -> str:
    try:
        out = _run_ps(
            f'Get-NetAdapter -Name "{adapter}" '
            f"| Select-Object -ExpandProperty Status"
        )
        return "已启用" if "Up" in out else "已禁用"
    except Exception:
        return "未知"


def disable_adapter(adapter: str):
    _run_ps(f'Disable-NetAdapter -Name "{adapter}" -Confirm:$false')


def enable_adapter(adapter: str):
    _run_ps(f'Enable-NetAdapter -Name "{adapter}" -Confirm:$false')


# ---------------------------------------------------------------------------
# 设置窗口 (PowerShell WinForms)
# ---------------------------------------------------------------------------

def show_settings_dialog():
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
            f'$txtAccount.Text = "{pure_account}"\n'
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
            f'$txtPass.Text = "{password}"\n'
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
    except Exception:
        return False
    finally:
        try:
            os.remove(ps_path)
        except Exception:
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
        sched = get_today_schedule(cfg)
        now = datetime.now()

        candidates = []
        if sched["disable"]:
            h, m = sched["disable"]
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            candidates.append(("disable", target))
        if sched["enable"]:
            h, m = sched["enable"]
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            candidates.append(("enable", target))

        if not candidates:
            # 今天没有定时任务，检查明天
            self._timer = threading.Timer(60, self._schedule_next)
            self._timer.daemon = True
            self._timer.start()
            return

        action_key, next_time = min(candidates, key=lambda x: x[1])
        delay = (next_time - now).total_seconds()
        action = self._on_disable if action_key == "disable" else self._on_enable

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
        self.scheduler.start()
        self._create_icon()

    # ---- 核心动作 --------------------------------------------------------

    def _do_disable(self):
        portal_logout(self.config["portal_logout_url"])
        disable_adapter(self.adapter)
        self._refresh()

    def _do_enable(self):
        enable_adapter(self.adapter)
        account, password = load_credentials()
        portal_login(account, password, self.config["portal_login_url"])
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
        status_text = f"以太网状态: {get_adapter_status(self.adapter)}"
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
        enabled = "启用" in get_adapter_status(self.adapter)
        self.icon.icon = make_icon_image(enabled)
        self.icon.menu = self._build_menu()

    def _on_disable(self, *_):
        threading.Thread(target=self._do_disable, daemon=True).start()

    def _on_enable(self, *_):
        threading.Thread(target=self._do_enable, daemon=True).start()

    def _on_settings(self, *_):
        threading.Thread(target=show_settings_dialog, daemon=True).start()

    def _on_toggle_autostart(self, *_):
        new_state = not is_autostart_enabled()
        set_autostart(new_state)

    def _on_reload(self, *_):
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
    TrayApp().run()


if __name__ == "__main__":
    main()
