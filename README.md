# 校园网定时断网助手 (NetSwitcher)

自动管理校园网以太网连接，定时断网/恢复，自动登录认证。

## 功能

- **定时断网/恢复**：按星期几独立配置，支持自定义时间
- **自动登录校园网**：启用以太网后自动完成 Dr.COM portal 认证
- **系统托盘常驻**：右键菜单查看状态、手动控制、账号设置
- **开机自启动**：通过任务计划程序实现，无 UAC 弹窗
- **断网时段保护**：断网时段内重启软件不会误触恢复

## 默认排程

| 日期 | 断网 | 恢复 |
|------|------|------|
| 周一 | 23:30 | 07:00 |
| 周二 | 23:30 | 07:00 |
| 周三 | 23:30 | 07:00 |
| 周四 | 23:30 | 07:00 |
| 周五 | 无 | 07:00 |
| 周六 | 无 | 无 |
| 周日 | 23:30 | 无 |

时间可在 `config.json` 中自定义，设为 `null` 表示当天不执行该动作。

## 使用方法

### 方式一：直接运行 exe

1. 从 [Releases](https://github.com/Vilan512/NetSwitcher/releases) 下载最新 zip
2. 解压得到 `NetSwitcher.exe`
3. 右键 → **以管理员身份运行**
4. 托盘右键 → **账号设置** → 输入校园网账号、密码、选择运营商
5. 可选：勾选 **开机自启动**

### 方式二：从源码运行

```bash
pip install pystray Pillow
python net_switcher.py
```

需要以管理员权限运行终端。

## config.json

程序启动时自动生成，可手动编辑：

```json
{
    "adapter": "以太网",
    "portal_login_url": "http://10.2.5.251:801/eportal/?c=Portal&a=login&login_method=1",
    "portal_logout_url": "http://10.2.5.251:801/eportal/?c=Portal&a=logout",
    "schedule": {
        "monday":    {"disable": "23:30", "enable": "07:00"},
        "tuesday":   {"disable": "23:30", "enable": "07:00"},
        "wednesday": {"disable": "23:30", "enable": "07:00"},
        "thursday":  {"disable": "23:30", "enable": "07:00"},
        "friday":    {"disable": null,    "enable": "07:00"},
        "saturday":  {"disable": null,    "enable": null},
        "sunday":    {"disable": "23:30", "enable": null}
    }
}
```

- `adapter`：Windows 网络连接中的网卡名称
- `portal_login_url` / `portal_logout_url`：校园网认证地址
- `schedule`：每天的断网/恢复时间，`null` 表示不执行
- 时间格式：`"HH:MM"`，24 小时制

修改后托盘右键 → **重新加载配置** 即可生效。

## 打包为 exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --uac-admin --name NetSwitcher net_switcher.py
```

产物在 `dist/NetSwitcher.exe`。

## 项目结构

```
net_switcher/
├── net_switcher.py      # 主程序
├── config.json          # 配置文件（自动生成）
├── test_selfcheck.py    # 自检脚本
├── requirements.txt     # Python 依赖
└── README.md
```

## 环境要求

- Windows 10/11
- 管理员权限（操作网卡和写注册表需要）
- PowerShell 5.1+
- Python 3.10+（仅源码运行时）

## License

MIT
