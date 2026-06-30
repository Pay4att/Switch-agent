#!/usr/bin/env python3
import asyncio
import base64
import os
import threading
from pathlib import Path

from flask import Flask, request, jsonify

from joycontrol.protocol import controller_protocol_factory
from joycontrol.server import create_hid_server
from joycontrol.controller import Controller
from joycontrol.controller_state import button_push
from joycontrol.transport import NotConnectedError


app = Flask(__name__)

DEFAULT_RECONNECT_BT_ADDR = os.environ.get(
    "SWITCH_RECONNECT_BT_ADDR",
    "78:81:8C:16:7B:A9",
)
AUTO_RECONNECT_ENABLED = os.environ.get("SWITCH_AUTO_RECONNECT", "1") not in {
    "0", "false", "False", "no", "NO"
}
DEFAULT_RECONNECT_TIMEOUT = float(
    os.environ.get("SWITCH_RECONNECT_TIMEOUT", "25")
)

# 全局异步事件循环
loop = asyncio.new_event_loop()

transport = None
protocol = None
controller_state = None
started = False
connected = False
starting = False
startup_task = None
last_error = None
controller_name_global = "PRO_CONTROLLER"
reconnect_bt_addr_global = DEFAULT_RECONNECT_BT_ADDR
device_id_global = None


VALID_BUTTONS = {
    "a", "b", "x", "y",
    "up", "down", "left", "right",
    "l", "r", "zl", "zr",
    "plus", "minus",
    "home", "capture",
    "left_stick", "right_stick",
}


def loop_worker():
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=loop_worker, daemon=True).start()


def run_async(coro, timeout=30):
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _format_start_error(exc):
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 98:
        return (
            "OSError(98, 'Address already in use'). "
            "The Bluetooth HID ports are busy. This usually means another joycontrol "
            "instance is already running, a previous /start call is still waiting for "
            "the Switch, or the BlueZ input plugin is conflicting. If this host is "
            "dedicated to joycontrol, disable the plugin with "
            "'--noplugin=input' and restart bluetooth."
        )

    return repr(exc)


def _consume_task_exception(task):
    if task.cancelled():
        return

    try:
        task.exception()
    except Exception:
        pass


def _current_phase():
    _sync_connection_state()

    if starting:
        return "starting"
    if started and controller_state is not None:
        return "ready" if connected else "started"
    if last_error:
        return "error"
    return "idle"


def _is_transport_active():
    proto_transport = getattr(protocol, "transport", None) if protocol is not None else None
    if proto_transport is None:
        return False

    is_closing = getattr(proto_transport, "is_closing", None)
    if callable(is_closing):
        try:
            if is_closing():
                return False
        except Exception:
            return False

    return True


def _sync_connection_state():
    global connected

    if connected and not _is_transport_active():
        connected = False

    return connected


def _looks_like_disconnect(exc):
    if isinstance(exc, (NotConnectedError, ConnectionResetError, BrokenPipeError)):
        return True

    text = repr(exc)
    markers = (
        "NotConnectedError",
        "Transport not registered",
        "No data received",
        "Connection lost",
        "Broken pipe",
        "Connection reset",
        "ConnectionAbortedError",
    )
    return any(marker in text for marker in markers)


async def _ensure_connected_async(timeout=None):
    global connected

    if startup_task is not None:
        if timeout is None:
            await asyncio.shield(startup_task)
        else:
            await asyncio.wait_for(asyncio.shield(startup_task), timeout=timeout)

    if controller_state is None:
        if last_error:
            raise RuntimeError(last_error)
        raise RuntimeError("controller not started")

    _sync_connection_state()

    if timeout is None:
        await controller_state.connect()
    else:
        await asyncio.wait_for(controller_state.connect(), timeout=timeout)

    connected = True


async def _bootstrap_controller_async(
    controller_name="PRO_CONTROLLER",
    device_id=None,
    reconnect_bt_addr=None
):
    global transport, protocol, controller_state, started, connected, starting, last_error

    effective_reconnect_bt_addr = reconnect_bt_addr or reconnect_bt_addr_global

    controller = Controller.from_arg(controller_name)
    factory = controller_protocol_factory(controller)

    kwargs = {}

    if device_id is not None:
        kwargs["device_id"] = device_id

    if effective_reconnect_bt_addr:
        kwargs["reconnect_bt_addr"] = effective_reconnect_bt_addr

    try:
        transport, protocol = await create_hid_server(factory, **kwargs)
        controller_state = protocol.get_controller_state()
        await controller_state.connect()
        connected = True
        last_error = None
        return "connected"
    except asyncio.CancelledError:
        transport = None
        protocol = None
        controller_state = None
        started = False
        connected = False
        last_error = "startup cancelled"
        raise
    except Exception as exc:
        transport = None
        protocol = None
        controller_state = None
        started = False
        connected = False
        last_error = _format_start_error(exc)
        raise RuntimeError(last_error) from exc
    finally:
        starting = False


async def start_controller_async(
    controller_name="PRO_CONTROLLER",
    device_id=None,
    reconnect_bt_addr=None,
    force_restart=False
):
    global started, connected, starting, startup_task, last_error
    global controller_name_global, reconnect_bt_addr_global, device_id_global

    if force_restart:
        await stop_controller_async()

    if startup_task is not None and not startup_task.done():
        return "already_starting"

    if started and controller_state is not None and _is_transport_active():
        return "already_started"
    if started and controller_state is not None and not _is_transport_active():
        await stop_controller_async(clear_last_error=False)

    controller_name_global = controller_name
    device_id_global = device_id
    if reconnect_bt_addr:
        reconnect_bt_addr_global = reconnect_bt_addr
    started = True
    connected = False
    starting = True
    last_error = None

    startup_task = asyncio.create_task(
        _bootstrap_controller_async(
            controller_name=controller_name,
            device_id=device_id,
            reconnect_bt_addr=reconnect_bt_addr
        )
    )
    startup_task.add_done_callback(_consume_task_exception)

    return "starting"


async def wait_connected_async(timeout=60):
    try:
        await _ensure_connected_async(timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"wait timed out after {float(timeout):.1f}s; "
            "start pairing on the Switch and call /wait again"
        ) from exc
    return True


async def reconnect_controller_async(timeout=DEFAULT_RECONNECT_TIMEOUT):
    global last_error

    if not reconnect_bt_addr_global:
        raise RuntimeError("reconnect_bt_addr is empty; cannot auto reconnect")

    await stop_controller_async(clear_last_error=False)
    await start_controller_async(
        controller_name=controller_name_global,
        device_id=device_id_global,
        reconnect_bt_addr=reconnect_bt_addr_global,
        force_restart=False
    )
    await _ensure_connected_async(timeout=timeout)
    last_error = None

    return {
        "result": "reconnected",
        "controller": controller_name_global,
        "reconnect_bt_addr": reconnect_bt_addr_global,
        "connected": connected,
    }


async def _run_with_auto_reconnect(action_name, action_coro_factory):
    global last_error

    try:
        return await action_coro_factory()
    except Exception as exc:
        _sync_connection_state()

        if not AUTO_RECONNECT_ENABLED or not _looks_like_disconnect(exc):
            raise

        last_error = f"{action_name} lost connection: {exc!r}"
        await reconnect_controller_async(timeout=DEFAULT_RECONNECT_TIMEOUT)
        return await action_coro_factory()


async def press_button_async(button, sec=0.1):
    async def _action():
        await _ensure_connected_async()
        await button_push(controller_state, button, sec=float(sec))

    await _run_with_auto_reconnect("press", _action)


async def hold_button_async(button):
    async def _action():
        await _ensure_connected_async()

        controller_state.button_state.set_button(button, True)
        await controller_state.send()

    await _run_with_auto_reconnect("hold", _action)


async def release_button_async(button):
    async def _action():
        await _ensure_connected_async()

        controller_state.button_state.set_button(button, False)
        await controller_state.send()

    await _run_with_auto_reconnect("release", _action)


async def release_all_async():
    async def _action():
        await _ensure_connected_async()

        for button in VALID_BUTTONS:
            try:
                controller_state.button_state.set_button(button, False)
            except Exception:
                pass

        await controller_state.send()

    await _run_with_auto_reconnect("release_all", _action)


async def sequence_async(actions):
    async def _action():
        await _ensure_connected_async()

        for action in actions:
            button = action.get("button")
            sec = float(action.get("sec", 0.1))
            wait = float(action.get("wait", 0.2))

            if button not in VALID_BUTTONS:
                raise ValueError(f"invalid button: {button}")

            await button_push(controller_state, button, sec=sec)
            await asyncio.sleep(wait)

    await _run_with_auto_reconnect("sequence", _action)


async def set_nfc_async(file_path):
    async def _action():
        await _ensure_connected_async()

        path = Path(file_path).expanduser().resolve()

        if not path.exists():
            raise FileNotFoundError(str(path))

        with open(path, "rb") as f:
            content = f.read()

        if not hasattr(controller_state, "set_nfc"):
            raise RuntimeError("this joycontrol-kb version has no set_nfc()")

        controller_state.set_nfc(content)

        return {
            "file": str(path),
            "bytes": len(content)
        }

    return await _run_with_auto_reconnect("set_nfc_file", _action)


async def set_nfc_content_async(content, filename=None):
    async def _action():
        await _ensure_connected_async()

        if not hasattr(controller_state, "set_nfc"):
            raise RuntimeError("this joycontrol-kb version has no set_nfc()")

        controller_state.set_nfc(content)

        return {
            "file": filename or "<uploaded>",
            "bytes": len(content)
        }

    return await _run_with_auto_reconnect("set_nfc_content", _action)


async def clear_nfc_async():
    async def _action():
        await _ensure_connected_async()

        if not hasattr(controller_state, "set_nfc"):
            raise RuntimeError("this joycontrol-kb version has no set_nfc()")

        controller_state.set_nfc(None)

        return "nfc_removed"

    return await _run_with_auto_reconnect("clear_nfc", _action)


async def stop_controller_async(clear_last_error=True):
    global transport, protocol, controller_state, started, connected, starting, startup_task, last_error

    task = startup_task
    startup_task = None
    starting = False

    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    if transport is not None:
        try:
            await transport.close()
        except Exception:
            pass

    transport = None
    protocol = None
    controller_state = None
    started = False
    connected = False
    if clear_last_error:
        last_error = None

    return "stopped"


@app.route("/health", methods=["GET"])
def health():
    _sync_connection_state()

    return jsonify({
        "ok": True,
        "pid": os.getpid(),
        "started": started,
        "starting": starting,
        "connected": connected,
        "controller": controller_name_global,
        "reconnect_bt_addr": reconnect_bt_addr_global,
        "phase": _current_phase(),
        "last_error": last_error
    })


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}

    controller = data.get("controller", "PRO_CONTROLLER")
    device_id = data.get("device_id")
    reconnect_bt_addr = data.get("reconnect_bt_addr")
    force_restart = bool(data.get("force_restart", False))

    try:
        result = run_async(
            start_controller_async(
                controller_name=controller,
                device_id=device_id,
                reconnect_bt_addr=reconnect_bt_addr,
                force_restart=force_restart
            ),
            timeout=10
        )

        return jsonify({
            "ok": True,
            "result": result,
            "controller": controller,
            "reconnect_bt_addr": reconnect_bt_addr_global,
            "started": started,
            "starting": starting,
            "phase": _current_phase()
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e),
            "phase": _current_phase(),
            "last_error": last_error
        }), 500


@app.route("/reconnect", methods=["POST"])
def reconnect():
    data = request.get_json(silent=True) or {}
    timeout = float(data.get("timeout", DEFAULT_RECONNECT_TIMEOUT))

    try:
        result = run_async(reconnect_controller_async(timeout=timeout), timeout=timeout + 10)

        return jsonify({
            "ok": True,
            **result,
            "phase": _current_phase()
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e),
            "phase": _current_phase(),
            "last_error": last_error
        }), 500


@app.route("/wait", methods=["POST"])
def wait_connected():
    data = request.get_json(silent=True) or {}
    timeout = float(data.get("timeout", 60))

    try:
        run_async(wait_connected_async(timeout=timeout), timeout=timeout + 5)

        return jsonify({
            "ok": True,
            "started": started,
            "starting": starting,
            "connected": True,
            "phase": _current_phase()
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e),
            "phase": _current_phase(),
            "last_error": last_error
        }), 500


@app.route("/press", methods=["POST"])
def press():
    data = request.get_json(force=True)

    button = data.get("button")
    sec = float(data.get("sec", 0.1))

    if button not in VALID_BUTTONS:
        return jsonify({
            "ok": False,
            "error": f"invalid button: {button}",
            "valid_buttons": sorted(VALID_BUTTONS)
        }), 400

    try:
        run_async(press_button_async(button, sec), timeout=10)

        return jsonify({
            "ok": True,
            "button": button,
            "sec": sec
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/hold", methods=["POST"])
def hold():
    data = request.get_json(force=True)

    button = data.get("button")

    if button not in VALID_BUTTONS:
        return jsonify({
            "ok": False,
            "error": f"invalid button: {button}",
            "valid_buttons": sorted(VALID_BUTTONS)
        }), 400

    try:
        run_async(hold_button_async(button), timeout=10)

        return jsonify({
            "ok": True,
            "button": button,
            "state": "hold"
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/release", methods=["POST"])
def release():
    data = request.get_json(force=True)

    button = data.get("button")

    if button not in VALID_BUTTONS:
        return jsonify({
            "ok": False,
            "error": f"invalid button: {button}",
            "valid_buttons": sorted(VALID_BUTTONS)
        }), 400

    try:
        run_async(release_button_async(button), timeout=10)

        return jsonify({
            "ok": True,
            "button": button,
            "state": "release"
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/release_all", methods=["POST"])
def release_all():
    try:
        run_async(release_all_async(), timeout=10)

        return jsonify({
            "ok": True,
            "state": "all_released"
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/sequence", methods=["POST"])
def sequence():
    data = request.get_json(force=True)

    actions = data.get("actions", [])

    if not isinstance(actions, list):
        return jsonify({
            "ok": False,
            "error": "actions must be a list"
        }), 400

    try:
        timeout = max(10, len(actions) * 3)

        run_async(sequence_async(actions), timeout=timeout)

        return jsonify({
            "ok": True,
            "count": len(actions)
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/nfc", methods=["POST"])
def nfc():
    data = request.get_json(force=True)

    file_path = data.get("file")
    content_base64 = data.get("content_base64")
    filename = data.get("filename")

    if not file_path and not content_base64:
        return jsonify({
            "ok": False,
            "error": "missing file or content_base64"
        }), 400

    try:
        if content_base64:
            try:
                content = base64.b64decode(content_base64, validate=True)
            except Exception as exc:
                return jsonify({
                    "ok": False,
                    "error": f"invalid content_base64: {exc}"
                }), 400

            result = run_async(
                set_nfc_content_async(content, filename=filename),
                timeout=10
            )
        else:
            result = run_async(set_nfc_async(file_path), timeout=10)

        return jsonify({
            "ok": True,
            "nfc": result
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/nfc/remove", methods=["POST"])
def nfc_remove():
    try:
        result = run_async(clear_nfc_async(), timeout=10)

        return jsonify({
            "ok": True,
            "result": result
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


@app.route("/stop", methods=["POST"])
def stop():
    try:
        result = run_async(stop_controller_async(), timeout=10)

        return jsonify({
            "ok": True,
            "result": result
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise PermissionError("must run as root: sudo python3 api.py")

    app.run(
        host="0.0.0.0",
        port=8899,
        debug=False,
        threaded=True
    )
