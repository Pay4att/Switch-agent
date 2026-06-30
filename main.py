from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Literal

import requests
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_REMOTE_URL = "http://192.168.31.227:8899"
DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_NFC_DIR = BASE_DIR / "bin"
DEFAULT_REMOTE_NFC_DIR = "bin"
VALID_BUTTONS = (
    "a",
    "b",
    "x",
    "y",
    "up",
    "down",
    "left",
    "right",
    "l",
    "r",
    "zl",
    "zr",
    "plus",
    "minus",
    "home",
    "capture",
    "left_stick",
    "right_stick",
)
BUTTON_PATTERNS = {
    "a": (r"a键", r"(?<![a-z])a(?![a-z])"),
    "b": (r"b键", r"(?<![a-z])b(?![a-z])"),
    "x": (r"x键", r"(?<![a-z])x(?![a-z])"),
    "y": (r"y键", r"(?<![a-z])y(?![a-z])"),
    "up": (r"上键", r"上方向键", r"\bup\b"),
    "down": (r"下键", r"下方向键", r"\bdown\b"),
    "left": (r"左键", r"左方向键", r"\bleft\b"),
    "right": (r"右键", r"右方向键", r"\bright\b"),
    "l": (r"l键", r"左肩键", r"(?<![a-z])l(?![a-z])"),
    "r": (r"r键", r"右肩键", r"(?<![a-z])r(?![a-z])"),
    "zl": (r"zl键", r"左扳机", r"\bzl\b"),
    "zr": (r"zr键", r"右扳机", r"\bzr\b"),
    "plus": (r"plus键", r"加号键", r"\bplus\b"),
    "minus": (r"minus键", r"减号键", r"\bminus\b"),
    "home": (r"home键", r"\bhome\b", r"主页键", r"主菜单键"),
    "capture": (r"capture键", r"\bcapture\b", r"截图键", r"录屏键"),
    "left_stick": (r"左摇杆键", r"左摇杆按键", r"\bl3\b"),
    "right_stick": (r"右摇杆键", r"右摇杆按键", r"\br3\b"),
}
PRESS_KEYWORDS = ("按下", "按一下", "点一下", "按一次", "轻按", "tap", "press")
HOLD_KEYWORDS = ("长按", "按住", "一直按", "hold", "keepholding")
RELEASE_KEYWORDS = ("松开", "释放", "release")
RELEASE_ALL_KEYWORDS = ("释放全部", "松开全部", "全部释放", "全部松开", "releaseall")
ButtonName = Literal[
    "a",
    "b",
    "x",
    "y",
    "up",
    "down",
    "left",
    "right",
    "l",
    "r",
    "zl",
    "zr",
    "plus",
    "minus",
    "home",
    "capture",
    "left_stick",
    "right_stick",
]

SYSTEM_PROMPT = """You are a Nintendo Switch remote controller agent.

Use the provided tools instead of guessing.

Rules:
1. Check remote status before control when state is unclear.
2. If the controller is not started, call start_switch_controller first.
3. If a button or NFC action needs a live connection and connected=false, call wait_until_connected.
4. For NFC requests, use search_nfc_files or list_nfc_files first, then load_nfc_file.
5. If an NFC match is ambiguous, ask the user to choose. Do not guess.
6. If the user only asks for status, only call get_switch_status and do not start or wait.
7. Keep answers short and report the real tool result.
8. When listing NFC files, use the exact filenames from the tool output and respect the requested limit. Do not shorten filenames.
9. Chinese phrases like 按下, 按一下, 点一下, 按一次, press, tap mean a short press. Use press_switch_button for these.
10. Use long_press_switch_button only when the user explicitly says 长按, 按住, hold, keep holding.
11. load_nfc_file uploads the local NFC file bytes from the local bin directory to the remote controller. Do not assume the file already exists on the remote machine.

Examples:
- User: 查看当前远端状态
  Action: call get_switch_status only.
- User: 列出前3个NFC文件
  Action: call list_nfc_files with limit=3.
- User: 搜索黄昏相关NFC
  Action: call search_nfc_files with keyword="黄昏".
- User: 加载黄昏公主塞尔达这个NFC
  Action: call search_nfc_files first, then call load_nfc_file after confirming a single match.
- User: 按下home键
  Action: call press_switch_button with button="home".
- User: 长按home键
  Action: call long_press_switch_button with button="home".
"""


class SwitchRemoteError(RuntimeError):
    pass


@dataclass(slots=True)
class NFCEntry:
    name: str
    local_path: Path


class SwitchRemoteClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        request_timeout = self.timeout if timeout is None else timeout
        try:
            response = self.session.request(
                method=method,
                url=url,
                json=payload,
                timeout=request_timeout,
            )
        except requests.RequestException as exc:
            raise SwitchRemoteError(f"request failed for {url}: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            body = response.text.strip()
            raise SwitchRemoteError(
                f"{method} {path} returned non-json response: {body or '<empty>'}"
            )

        if not response.ok or data.get("ok") is False:
            error = data.get("error", f"HTTP {response.status_code}")
            raise SwitchRemoteError(f"{method} {path} failed: {error}")

        return data

    def health(self) -> dict:
        return self._request("GET", "/health")

    def start(
        self,
        controller: str = "PRO_CONTROLLER",
        device_id: int | None = None,
        reconnect_bt_addr: str | None = None,
        force_restart: bool = False,
    ) -> dict:
        payload = {
            "controller": controller,
            "force_restart": force_restart,
        }
        if device_id is not None:
            payload["device_id"] = device_id
        if reconnect_bt_addr:
            payload["reconnect_bt_addr"] = reconnect_bt_addr
        return self._request("POST", "/start", payload)

    def wait_until_connected(self, timeout: float = 120.0) -> dict:
        read_timeout = max(float(timeout) + 10.0, self.timeout)
        return self._request(
            "POST",
            "/wait",
            {"timeout": timeout},
            timeout=(5.0, read_timeout),
        )

    def press(self, button: str, sec: float = 0.1) -> dict:
        return self._request("POST", "/press", {"button": button, "sec": sec})

    def hold(self, button: str) -> dict:
        return self._request("POST", "/hold", {"button": button})

    def release(self, button: str) -> dict:
        return self._request("POST", "/release", {"button": button})

    def release_all(self) -> dict:
        return self._request("POST", "/release_all", {})

    def sequence(self, actions: list[dict]) -> dict:
        return self._request("POST", "/sequence", {"actions": actions})

    def set_nfc(self, remote_file: str) -> dict:
        return self._request("POST", "/nfc", {"file": remote_file})

    def set_nfc_content(self, filename: str, content: bytes) -> dict:
        payload = {
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        return self._request("POST", "/nfc", payload)

    def clear_nfc(self) -> dict:
        return self._request("POST", "/nfc/remove", {})

    def stop(self) -> dict:
        return self._request("POST", "/stop", {})


class NFCRepository:
    def __init__(self, local_dir: Path, remote_dir: str) -> None:
        self.local_dir = local_dir
        self.remote_dir = remote_dir.strip().strip("/\\") or "bin"

    def list_entries(self) -> list[NFCEntry]:
        if not self.local_dir.exists():
            return []

        entries: list[NFCEntry] = []
        for path in sorted(self.local_dir.glob("*.bin")):
            entries.append(NFCEntry(name=path.name, local_path=path.resolve()))
        return entries

    def search(self, query: str, limit: int = 10) -> list[NFCEntry]:
        entries = self.list_entries()
        if not query.strip():
            return entries[:limit]

        normalized_query = self._normalize(query)
        exact_matches = []
        partial_matches = []

        for entry in entries:
            name_key = self._normalize(entry.name)
            stem_key = self._normalize(Path(entry.name).stem)
            if normalized_query in {name_key, stem_key}:
                exact_matches.append(entry)
            elif normalized_query and (
                normalized_query in name_key or normalized_query in stem_key
            ):
                partial_matches.append(entry)

        if exact_matches:
            return exact_matches[:limit]
        if partial_matches:
            return partial_matches[:limit]

        choices = {
            self._normalize(entry.name): entry for entry in entries
        } | {
            self._normalize(Path(entry.name).stem): entry for entry in entries
        }
        close_keys = get_close_matches(
            normalized_query,
            list(choices.keys()),
            n=limit,
            cutoff=0.25,
        )
        return [choices[key] for key in close_keys]

    def resolve_one(self, query: str) -> tuple[NFCEntry | None, list[NFCEntry]]:
        matches = self.search(query, limit=10)
        if len(matches) == 1:
            return matches[0], matches
        return None, matches

    @staticmethod
    def _normalize(text: str) -> str:
        chunks = []
        for char in text.casefold():
            if char.isspace():
                continue
            category = unicodedata.category(char)
            if category.startswith(("P", "S")):
                continue
            chunks.append(char)
        return "".join(chunks)


def list_ollama_models() -> list[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    models = []
    for line in result.stdout.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        models.append(stripped.split()[0])
    return models


def resolve_model_name(preferred: str) -> str:
    available = list_ollama_models()
    if not available or preferred in available:
        return preferred

    fallback = DEFAULT_MODEL if DEFAULT_MODEL in available else available[0]
    print(
        f"[warn] Ollama model '{preferred}' is not installed. Falling back to '{fallback}'.",
        file=sys.stderr,
    )
    return fallback


def truncate_items(items: list[str], limit: int) -> list[str]:
    return items[: max(limit, 1)]


def _normalize_command_text(text: str) -> tuple[str, str]:
    lowered = text.casefold()
    compact = re.sub(r"\s+", "", lowered)
    return lowered, compact


def match_direct_button_command(prompt: str) -> tuple[str, str | None] | None:
    lowered, compact = _normalize_command_text(prompt)

    if any(keyword in compact for keyword in RELEASE_ALL_KEYWORDS):
        return "release_all", None

    action = None
    if any(keyword in compact for keyword in HOLD_KEYWORDS):
        action = "hold"
    elif any(keyword in compact for keyword in RELEASE_KEYWORDS):
        action = "release"
    elif any(keyword in compact for keyword in PRESS_KEYWORDS):
        action = "press"

    if action is None:
        return None

    matched_buttons = []
    for button, patterns in BUTTON_PATTERNS.items():
        if any(re.search(pattern, lowered) for pattern in patterns):
            matched_buttons.append(button)

    if len(matched_buttons) != 1:
        return None

    return action, matched_buttons[0]


def ensure_remote_ready(client: SwitchRemoteClient) -> None:
    health = client.health()
    if not health.get("started") and not health.get("starting"):
        client.start()
        health = client.health()
    if not health.get("connected"):
        client.wait_until_connected()


def try_run_direct_command(client: SwitchRemoteClient, prompt: str) -> str | None:
    matched = match_direct_button_command(prompt)
    if matched is None:
        return None

    action, button = matched

    if action == "release_all":
        ensure_remote_ready(client)
        client.release_all()
        return "已释放所有按键。"

    if button is None:
        return None

    ensure_remote_ready(client)

    if action == "press":
        client.press(button=button, sec=0.1)
        return f"已短按 {button} 键。"
    if action == "hold":
        client.hold(button=button)
        return f"已长按 {button} 键。"
    if action == "release":
        client.release(button=button)
        return f"已松开 {button} 键。"

    return None


def build_tools(client: SwitchRemoteClient, nfc_repo: NFCRepository) -> list:
    @tool
    def get_switch_status() -> dict:
        """Get the current remote controller status."""
        return client.health()

    @tool
    def start_switch_controller(
        controller: str = "PRO_CONTROLLER",
        reconnect_bt_addr: str | None = None,
        device_id: int | None = None,
        force_restart: bool = False,
    ) -> dict:
        """Start the remote Switch controller server-side controller instance."""
        return client.start(
            controller=controller,
            device_id=device_id,
            reconnect_bt_addr=reconnect_bt_addr,
            force_restart=force_restart,
        )

    @tool
    def wait_until_connected(timeout: float = 120.0) -> dict:
        """Wait until the remote Switch controller is connected to the console."""
        return client.wait_until_connected(timeout=timeout)

    @tool
    def press_switch_button(button: ButtonName, sec: float = 0.1) -> dict:
        """Short press one button. Use this for 按下, 按一下, 点一下, 按一次, press, tap. Do not use it for long hold requests."""
        return client.press(button=button, sec=sec)

    @tool
    def long_press_switch_button(button: ButtonName) -> dict:
        """Hold one button continuously. Use this only for explicit 长按, 按住, hold, or keep holding requests."""
        return client.hold(button=button)

    @tool
    def release_switch_button(button: ButtonName) -> dict:
        """Release one held Switch button."""
        return client.release(button=button)

    @tool
    def release_all_switch_buttons() -> dict:
        """Release all held buttons on the remote controller."""
        return client.release_all()

    @tool
    def press_button_sequence(
        buttons: list[ButtonName],
        press_sec: float = 0.1,
        wait_sec: float = 0.2,
    ) -> dict:
        """Press a sequence of valid buttons using the same press and wait duration."""
        actions = [
            {"button": button, "sec": press_sec, "wait": wait_sec} for button in buttons
        ]
        return client.sequence(actions)

    @tool
    def list_nfc_files(limit: int = 30) -> dict:
        """List NFC .bin files. Use this when the user asks to list, show, browse, or view the first N NFC files."""
        entries = nfc_repo.list_entries()
        names = [entry.name for entry in entries]
        return {
            "count": len(names),
            "files": truncate_items(names, limit),
            "remote_dir": nfc_repo.remote_dir,
        }

    @tool
    def search_nfc_files(keyword: str, limit: int = 10) -> dict:
        """Search NFC files by keyword. Use this only when the user gives a name fragment such as 黄昏, 林克, 塞尔达, 米法."""
        matches = nfc_repo.search(keyword, limit=limit)
        return {
            "keyword": keyword,
            "count": len(matches),
            "matches": [entry.name for entry in matches],
        }

    @tool
    def load_nfc_file(name: str) -> dict:
        """Load one NFC file to the remote controller by local bin filename or partial name. This uploads the local file bytes from the local bin directory."""
        match, matches = nfc_repo.resolve_one(name)
        if match is None:
            return {
                "ok": False,
                "error": "nfc file is missing or ambiguous",
                "query": name,
                "matches": [entry.name for entry in matches],
            }
        content = match.local_path.read_bytes()
        result = client.set_nfc_content(match.name, content)
        result["selected"] = match.name
        result["local_file"] = str(match.local_path)
        return result

    @tool
    def unload_nfc_file() -> dict:
        """Remove the currently loaded NFC data from the remote controller."""
        return client.clear_nfc()

    @tool
    def stop_switch_controller() -> dict:
        """Stop the remote controller instance."""
        return client.stop()

    return [
        get_switch_status,
        start_switch_controller,
        wait_until_connected,
        press_switch_button,
        long_press_switch_button,
        release_switch_button,
        release_all_switch_buttons,
        press_button_sequence,
        list_nfc_files,
        search_nfc_files,
        load_nfc_file,
        unload_nfc_file,
        stop_switch_controller,
    ]


def create_switch_agent(
    remote_url: str,
    model_name: str,
    ollama_base_url: str | None,
    local_nfc_dir: Path,
    remote_nfc_dir: str,
):
    client = SwitchRemoteClient(remote_url)
    nfc_repo = NFCRepository(local_nfc_dir, remote_nfc_dir)
    model = ChatOllama(
        model=resolve_model_name(model_name),
        base_url=ollama_base_url,
        temperature=0,
    )
    tools = build_tools(client, nfc_repo)
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        name="switch_remote_agent",
    )


def extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict) and item.get("text"):
                chunks.append(str(item["text"]))
        return "\n".join(part.strip() for part in chunks if part).strip()
    return str(content).strip()


def extract_last_ai_message(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = extract_text(message.content)
            if text:
                return text
    return ""


def run_agent_prompt(
    agent,
    client: SwitchRemoteClient,
    messages: list,
    prompt: str,
) -> tuple[list, str]:
    direct_text = try_run_direct_command(client, prompt)
    if direct_text is not None:
        next_messages = [*messages, HumanMessage(content=prompt), AIMessage(content=direct_text)]
        return next_messages, direct_text

    next_input = [*messages, HumanMessage(content=prompt)]
    result = agent.invoke({"messages": next_input})
    next_messages = result["messages"]
    text = extract_last_ai_message(next_messages)
    return next_messages, text or "(no assistant text returned)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LangChain create_agent based Nintendo Switch remote controller."
    )
    parser.add_argument(
        "--remote-url",
        default=DEFAULT_REMOTE_URL,
        help=f"Remote controller API base URL. Default: {DEFAULT_REMOTE_URL}",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama model name. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--ollama-base-url",
        default=None,
        help="Optional custom Ollama base URL, for example http://127.0.0.1:11434",
    )
    parser.add_argument(
        "--local-nfc-dir",
        default=str(DEFAULT_NFC_DIR),
        help=f"Local directory used to discover NFC .bin files. Default: {DEFAULT_NFC_DIR}",
    )
    parser.add_argument(
        "--remote-nfc-dir",
        default=DEFAULT_REMOTE_NFC_DIR,
        help="Remote NFC directory passed to /nfc as a relative path. Default: bin",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Run one prompt and exit. Without this flag an interactive shell is started.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    agent = create_switch_agent(
        remote_url=args.remote_url,
        model_name=args.model,
        ollama_base_url=args.ollama_base_url,
        local_nfc_dir=Path(args.local_nfc_dir),
        remote_nfc_dir=args.remote_nfc_dir,
    )

    client = SwitchRemoteClient(args.remote_url)
    try:
        health = client.health()
        print(json.dumps(health, ensure_ascii=False))
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"[warn] remote health check failed: {exc}", file=sys.stderr)

    messages: list = []
    if args.prompt:
        messages, text = run_agent_prompt(agent, client, messages, args.prompt)
        print(text)
        return 0

    print("switch-remote agent ready. Type 'exit' to quit.")
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            return 0

        try:
            messages, text = run_agent_prompt(agent, client, messages, prompt)
            print(f"agent> {text}")
        except Exception as exc:
            print(f"agent> error: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
