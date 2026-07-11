from __future__ import annotations

import os
import json
from contextlib import contextmanager
from pathlib import Path


if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.lock_path.touch(exist_ok=True)
        except PermissionError:
            self.lock_path = None
        if not self.path.exists():
            self.save({"cycles": {}})

    @contextmanager
    def _locked(self):
        if self.lock_path is None:
            yield
            return
        with self.lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()

            lock_file.seek(0)
            self._acquire_lock(lock_file)
            try:
                yield
            finally:
                self._release_lock(lock_file)

    @staticmethod
    def _acquire_lock(lock_file) -> None:
        if os.name == "nt":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _release_lock(lock_file) -> None:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_state(self) -> dict:
        if not self.path.exists():
            return {"cycles": {}}

        text = self.path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"State file is corrupted: {self.path} ({exc})") from exc

    def _write_state(self, state: dict) -> None:
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp_path, self.path)

    def _update_state(self, updater) -> None:
        with self._locked():
            state = self._read_state()
            updater(state)
            self._write_state(state)

    def load(self) -> dict:
        with self._locked():
            return self._read_state()

    def save(self, state: dict) -> None:
        with self._locked():
            self._write_state(state)

    def get_cycles(self) -> dict[str, dict]:
        state = self.load()
        return dict(state.get("cycles", {}))

    def get_asset_controls(self) -> dict[str, dict]:
        state = self.load()
        return dict(state.get("asset_controls", {}))

    def get_profit_history(self) -> dict[str, list[dict]]:
        state = self.load()
        history = state.get("profit_history", {})
        return {
            str(asset).upper(): [dict(sample) for sample in samples if isinstance(sample, dict)]
            for asset, samples in history.items()
            if isinstance(samples, list)
        }

    def get_portfolio_value_target(self) -> dict | None:
        state = self.load()
        target = state.get("portfolio_value_target")
        if not isinstance(target, dict):
            return None
        return dict(target)

    def get_whole_strategy_profit_target(self) -> dict | None:
        state = self.load()
        target = state.get("whole_strategy_profit_target")
        if not isinstance(target, dict):
            return None
        return dict(target)

    def set_asset_control(self, asset: str, control: dict) -> None:
        def updater(state: dict) -> None:
            controls = dict(state.get("asset_controls", {}))
            controls[asset.upper()] = control
            state["asset_controls"] = controls

        self._update_state(updater)

    def delete_asset_control(self, asset: str) -> None:
        def updater(state: dict) -> None:
            controls = dict(state.get("asset_controls", {}))
            controls.pop(asset.upper(), None)
            state["asset_controls"] = controls

        self._update_state(updater)

    def get_telegram_update_offset(self) -> int | None:
        state = self.load()
        value = state.get("telegram_update_offset")
        if value is None:
            return None
        return int(value)

    def set_telegram_update_offset(self, offset: int) -> None:
        def updater(state: dict) -> None:
            state["telegram_update_offset"] = int(offset)

        self._update_state(updater)

    def append_profit_sample(self, asset: str, sample: dict, limit: int = 96) -> None:
        def updater(state: dict) -> None:
            history = {
                str(name).upper(): [dict(item) for item in samples if isinstance(item, dict)]
                for name, samples in state.get("profit_history", {}).items()
                if isinstance(samples, list)
            }
            asset_key = asset.upper()
            samples = history.get(asset_key, [])
            samples.append(dict(sample))
            history[asset_key] = samples[-max(2, int(limit)):]
            state["profit_history"] = history

        self._update_state(updater)

    def set_portfolio_value_target(self, target: dict) -> None:
        def updater(state: dict) -> None:
            state["portfolio_value_target"] = dict(target)

        self._update_state(updater)

    def set_whole_strategy_profit_target(self, target: dict) -> None:
        def updater(state: dict) -> None:
            state["whole_strategy_profit_target"] = dict(target)

        self._update_state(updater)

    def clear_portfolio_value_target(self) -> None:
        def updater(state: dict) -> None:
            state.pop("portfolio_value_target", None)

        self._update_state(updater)

    def clear_whole_strategy_profit_target(self) -> None:
        def updater(state: dict) -> None:
            state.pop("whole_strategy_profit_target", None)

        self._update_state(updater)

    def upsert_cycle(self, symbol: str, cycle: dict) -> None:
        def updater(state: dict) -> None:
            cycles = dict(state.get("cycles", {}))
            cycles[symbol] = cycle
            state["cycles"] = cycles

        self._update_state(updater)

    def delete_cycle(self, symbol: str) -> None:
        def updater(state: dict) -> None:
            cycles = dict(state.get("cycles", {}))
            cycles.pop(symbol, None)
            state["cycles"] = cycles

        self._update_state(updater)

    # ------------------------------------------------------------------
    # Bitget mirror orders
    # ------------------------------------------------------------------
    def get_bitget_orders(self) -> list[dict]:
        state = self.load()
        orders = state.get("bitget_orders", [])
        if not isinstance(orders, list):
            return []
        return [dict(o) for o in orders if isinstance(o, dict)]

    def append_bitget_order(self, order: dict, limit: int = 200) -> None:
        def updater(state: dict) -> None:
            orders = state.get("bitget_orders", [])
            if not isinstance(orders, list):
                orders = []
            orders.append(dict(order))
            state["bitget_orders"] = orders[-max(2, int(limit)):]

        self._update_state(updater)

    def update_bitget_order(self, order_id: str, updates: dict) -> None:
        def updater(state: dict) -> None:
            orders = state.get("bitget_orders", [])
            if not isinstance(orders, list):
                return
            for entry in orders:
                if isinstance(entry, dict) and entry.get("order_id") == order_id:
                    entry.update(updates)
                    break
            state["bitget_orders"] = orders

        self._update_state(updater)
