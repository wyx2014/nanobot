import asyncio
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.bus.events import InboundMessage
from nanobot.command.t46u import DialIntent, _request, execute_dial_intent, parse_dial_intent


class DialIntentTest(unittest.TestCase):
    def test_direct_requests(self):
        self.assertEqual(parse_dial_intent("拨打张三电话"), DialIntent("dial", "张三"))
        self.assertEqual(parse_dial_intent("用我的电话拨打张三"), DialIntent("dial", "张三"))
        self.assertEqual(parse_dial_intent("使用skill技能拨打12121"), DialIntent("dial", "12121"))
        self.assertEqual(parse_dial_intent("拨号给张三"), DialIntent("dial", "张三"))
        self.assertEqual(parse_dial_intent("给张三打电话"), DialIntent("dial", "张三"))
        self.assertEqual(parse_dial_intent("用这个技能给张三打电话"), DialIntent("dial", "张三"))
        self.assertEqual(parse_dial_intent("请用3901分机拨打12121"), DialIntent("dial", "12121", "3901"))
        self.assertEqual(parse_dial_intent("请挂断电话"), DialIntent("hangup"))
        self.assertEqual(parse_dial_intent("/call 张三"), DialIntent("dial", "张三"))
        self.assertEqual(parse_dial_intent("/call 12121"), DialIntent("dial", "12121"))
        self.assertEqual(parse_dial_intent("/call 挂断"), DialIntent("hangup"))
        self.assertEqual(parse_dial_intent("/call"), DialIntent("help"))

    def test_discussion_is_not_a_call(self):
        for text in (
            "怎么拨打电话？", "分析一下拨打12121为什么失败", "帮我写一个拨打电话的脚本",
            "拨打张三电话为什么失败", "拨打张三？",
        ):
            self.assertIsNone(parse_dial_intent(text))

    def test_uses_call_config_and_legacy_directory_without_network_during_lookup_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "skills" / "call").mkdir(parents=True)
            (workspace / "skills" / "t46u-dial").mkdir(parents=True)
            (workspace / "skills" / "call" / "config.json").write_text(json.dumps({
                "caller": "3901", "phone_ip": "192.0.2.50", "pbx_ip": "12.100.10.9",
                "username": "admin", "password": "configured locally",
            }))
            (workspace / "skills" / "t46u-dial" / "内部分机信息表.md").write_text(
                "| 分机短号 | 显示名称 | 显示名称(英文) | 终端IP |\n"
                "| --- | --- | --- | --- |\n"
                "| 3901 | 主叫 | Caller | 192.0.2.50 |\n"
                "| 3737 | 张三 | Zhang San | 192.0.2.98 |\n",
                encoding="utf-8",
            )
            with patch("nanobot.command.t46u._request", return_value=(200, "OK")) as request:
                reply = execute_dial_intent(workspace, DialIntent("dial", "张三"))
                self.assertIn("已向", reply)
                self.assertEqual(request.call_args.args[0], "192.0.2.50")
                self.assertEqual(request.call_args.args[3], {
                    "number": "3737", "outgoing_url": "3901@12.100.10.9",
                })
                execute_dial_intent(workspace, DialIntent("dial", "12121"))
                self.assertEqual(request.call_args.args[3]["number"], "912121")
                execute_dial_intent(workspace, DialIntent("dial", "12121", "主叫"))
                self.assertEqual(request.call_args.args[3]["outgoing_url"], "3901@12.100.10.9")
                self.assertIn("没有", execute_dial_intent(workspace, DialIntent("dial", "不存在")))
                self.assertEqual(request.call_count, 3)

    def test_new_call_skill_and_local_config_take_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / ".nanobot").mkdir()
            new_dir = workspace / "skills" / "call"
            new_dir.mkdir(parents=True)
            (workspace / ".nanobot" / "call.json").write_text(json.dumps({
                "caller": "9999", "phone_ip": "192.0.2.99", "username": "old", "password": "old",
            }))
            (new_dir / "config.json").write_text(json.dumps({
                "caller": "3902", "phone_ip": "192.0.2.51", "pbx_ip": "12.100.10.9",
                "username": "admin", "password": "configured locally", "directory": "contacts.md",
            }))
            (new_dir / "contacts.md").write_text(
                "| 分机短号 | 显示名称 | 显示名称(英文) | 终端IP |\n"
                "| --- | --- | --- | --- |\n"
                "| 3738 | 张三 | Zhang San | 192.0.2.99 |\n",
                encoding="utf-8",
            )
            with patch("nanobot.command.t46u._request", return_value=(200, "OK")) as request:
                reply = execute_dial_intent(workspace, DialIntent("dial", "张三"))
            self.assertIn("已向", reply)
            self.assertEqual(request.call_args.args[0], "192.0.2.51")
            self.assertEqual(request.call_args.args[3]["outgoing_url"], "3902@12.100.10.9")
            self.assertEqual(request.call_args.args[3]["number"], "3738")
            self.assertIn(
                {"name": "call", "path": str(SkillsLoader(workspace).builtin_skills / "call" / "SKILL.md"), "source": "builtin"},
                SkillsLoader(workspace).list_skills(),
            )

    def test_bundled_directory_resolves_name_when_local_table_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            skill_dir = workspace / "skills" / "call"
            skill_dir.mkdir(parents=True)
            (skill_dir / "config.json").write_text(json.dumps({
                "caller": "3960", "username": "admin", "password": "configured locally",
            }))
            bundled_table = root / "内部分机信息表.md"
            bundled_table.write_text(
                "| 分机短号 | 显示名称 | 显示名称(英文) | 终端IP | 分机注册状态 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 3960 | 主叫 | Caller | 192.0.2.60 | 在线 |\n"
                "| 3737 | 张三 | Zhang San | 192.0.2.98 | 在线 |\n",
                encoding="utf-8",
            )
            with patch("nanobot.command.t46u.BUNDLED_DIRECTORY", bundled_table), patch(
                "nanobot.command.t46u._request", return_value=(200, "OK")
            ) as request:
                reply = execute_dial_intent(workspace, DialIntent("dial", "张三"))
            self.assertIn("已向", reply)
            self.assertEqual(request.call_args.args[0], "192.0.2.60")
            self.assertEqual(request.call_args.args[3], {
                "number": "3737", "outgoing_url": "3960@12.100.10.9",
            })

    def test_placeholder_directory_never_dials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            skill_dir = workspace / "skills" / "call"
            skill_dir.mkdir(parents=True)
            (skill_dir / "config.json").write_text(json.dumps({
                "caller": "3960", "phone_ip": "192.0.2.60",
                "username": "admin", "password": "configured locally",
            }))
            bundled_table = root / "内部分机信息表.md"
            bundled_table.write_text(
                "| 分机短号 | 显示名称 | 显示名称(英文) | 终端IP | 分机注册状态 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| xxx | 阮三 | Ruan San | xxx.xxx.1.1 | 在线 |\n",
                encoding="utf-8",
            )
            with patch("nanobot.command.t46u.BUNDLED_DIRECTORY", bundled_table), patch(
                "nanobot.command.t46u._request"
            ) as request:
                reply = execute_dial_intent(workspace, DialIntent("dial", "阮三"))
            self.assertIn("没有有效的四位分机号", reply)
            request.assert_not_called()

    def test_missing_caller_is_clear_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as temp, patch("nanobot.command.t46u._request") as request:
            reply = execute_dial_intent(Path(temp), DialIntent("dial", "12121"))
            self.assertIn("主叫分机", reply)
            request.assert_not_called()

    def test_old_nanobot_config_paths_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp, patch("nanobot.command.t46u._request") as request:
            workspace = Path(temp)
            legacy_dir = workspace / ".nanobot"
            legacy_dir.mkdir()
            config = json.dumps({
                "caller": "3901", "phone_ip": "192.0.2.50",
                "username": "admin", "password": "legacy",
            })
            (legacy_dir / "call.json").write_text(config)
            (legacy_dir / "t46u-dial.json").write_text(config)
            reply = execute_dial_intent(workspace, DialIntent("dial", "12121"))
            self.assertIn("skills/call/config.json", reply)
            request.assert_not_called()

    def test_phone_request_uses_configmanapp_and_bypasses_host_proxy(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b"OK"
        with patch("nanobot.command.t46u.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(_request(
                "192.0.2.50", "admin", "secret",
                {"number": "912121", "outgoing_url": "3901@12.100.10.9"}, scheme="https",
            ), (200, "OK"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url.split("?")[0], "https://192.0.2.50/cgi-bin/ConfigManApp.com")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query), {
            "number": ["912121"], "outgoing_url": ["3901@12.100.10.9"],
        })
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_legacy_global_default_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp, patch("nanobot.command.t46u._request") as request:
            workspace = Path(temp)
            skill_dir = workspace / "skills" / "t46u-dial"
            skill_dir.mkdir(parents=True)
            (skill_dir / "user_map.json").write_text('{"default":"3960"}')
            reply = execute_dial_intent(workspace, DialIntent("dial", "12121"))
            self.assertIn("主叫分机", reply)
            request.assert_not_called()

    def test_desktop_turn_uses_phone_route_before_model_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            dispatch = AsyncMock(return_value=None)
            session = Mock()
            loop = SimpleNamespace(
                workspace=Path(temp),
                context=SimpleNamespace(skills=SimpleNamespace(list_skills=Mock(return_value=[
                    {"name": "call"},
                ]))),
                commands=SimpleNamespace(dispatch=dispatch),
                sessions=SimpleNamespace(save=Mock()),
                _persist_user_message_early=Mock(return_value=True),
                _clear_pending_user_turn=Mock(),
            )
            ctx = SimpleNamespace(
                msg=InboundMessage(channel="websocket", sender_id="user", chat_id="chat", content="拨打12121"),
                session=session, session_key="websocket:chat", outbound=None,
                user_persisted_early=False,
            )
            with patch.dict(os.environ, {"NANOBOT_DESKTOP_GATEWAY": "1"}), patch(
                "nanobot.agent.loop.execute_dial_intent", return_value="指令已发送"
            ) as dial:
                self.assertEqual(asyncio.run(AgentLoop._state_command(loop, ctx)), "shortcut")
            self.assertEqual(ctx.outbound.content, "指令已发送")
            dial.assert_called_once()
            dispatch.assert_not_awaited()
            session.add_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
