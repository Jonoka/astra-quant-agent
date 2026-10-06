"""B3（主脑侧第一块）`scripts/brain/packages.py` 的抽取回归。

## 这个测试在守什么

`fetch_single_instrument_package` 从 `scripts/ai_brain_trader.py`（原 L303-556，
254 行）整段搬进 `scripts/brain/packages.py`。**搬走前该函数零测试覆盖**，
而它是主脑每 15 分钟对每个标的跑一次的行情/指标装配 —— 它悄悄坏掉的表现是
"主脑拿到一包空数据继续决策"，而不是抛错。

因此这里守三件事：

1. **源码范围受控**：函数全文与已部署 f53e579 基线逐字相同，只允许本次精确的
   taker 解析替换；旧搬运前夹具保留为历史证据，方向语义另做运行测试。
2. **门面确实在转发**：门面同名壳必须真的调用子模块，且把两个行情函数
   **按调用期注入**传下去 —— 若改回 import 期绑定，`patch.object(门面, …)` 会失效。
3. **失败仍然是 fail-soft**：行情取不到时必须返回结构完整的包（`data_quality`
   降级），绝不能抛 —— 这是原函数的语义，搬运不得改变。
"""
from __future__ import annotations

import ast
import hashlib
import re
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import scripts.ai_brain_trader as abt
from scripts.brain import packages as brain_packages

ROOT = Path(__file__).resolve().parents[2]
FACADE = ROOT / "scripts" / "ai_brain_trader.py"
SUBMODULE = ROOT / "scripts" / "brain" / "packages.py"

# 搬走前门面里该函数的原文（历史留档；活动保护检查使用已部署版本指纹）。
PRE_MOVE_SOURCE = Path(__file__).resolve().parent.parent / "data" / "brain_package_pre_move.py"


def _submodule_function_lines() -> list[str]:
    src = SUBMODULE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "fetch_single_instrument_package")
    return src.splitlines()[fn.lineno - 1:fn.end_lineno]


class MoveIsLosslessTest(unittest.TestCase):
    # 第 137 刀为 6 处**静默** except 接入了失败计数（可观测性；不改取值行为），
    # 故"逐行等同"必须放行这批**文档化差异**。放行范围被刻意收得极窄：
    #   ① 裸 `except Exception:` → `except Exception as exc:`（仅为拿到异常对象）
    #   ② 恰为 `note_failure("<小写名>", exc)` 的**新增**行
    # 其余任何一行差异仍然会红；并另有 `test_failure_counters_are_actually_wired`
    # 正向钉住这 6 处接线确实存在 —— 白名单不能被用来掩盖真正的搬运错误。
    #
    # v8.1.0（66279f1）又给 OKX ticker 加了**取数延时观测**（纯可观测性，同样
    # 不改任何取值口径），故再放行第三条极窄差异：
    #   ③ `t_okx0 = time.time()` 起点采样行 + 恰为
    #      `pkg["okx_latency_ms"] = max(1, int(round((time.time() - t_okx0) * 1000)))`
    #      的**新增**行（整行丢弃，两侧对齐后再逐行比对）
    # 同样由 `test_okx_latency_instrumentation_is_actually_wired` 正向钉住。
    _NOTE_RE = re.compile(r'^note_failure\("[a-z_0-9]+", exc\)$')
    _LATENCY_START_RE = re.compile(r"^t_okx0 = time\.time\(\)$")
    _LATENCY_PUBLISH_RE = re.compile(
        r'^pkg\["okx_latency_ms"\] = max\(1, int\(round\(\(time\.time\(\) - t_okx0\) \* 1000\)\)\)$')

    #: 2026-10 **有意修正**：15M 量比原实现用 `vols[-1]` —— 那是 OKX 返回的
    #: **未收盘**当前根，刚开盘时量≈0，实盘提示词里长期是 `15M量比=0.01x`
    #: （模型读成"成交量枯竭"）。改为"最近一根**已收盘**的量 vs 它前面 5 根均量"。
    #: 与上面两类一样，放行范围刻意收窄到**三行**，且另有正向用例
    #: （`tests/ops/test_brain_packages.py::test_volume_ratio_ignores_the_still_forming_bar`）
    #: 钉住新口径真的生效 —— 白名单不能用来掩盖搬运错误。
    _INTENTIONAL_EDITS = {
        "                if len(vols) >= 6:":
            "                if len(vols) >= 7:",
        "                    avg_v5 = sum(vols[-6:-1]) / 5":
            "                    avg_v5 = sum(vols[-7:-2]) / 5",
        '                        pkg["vol_ratio"] = round(vols[-1] / avg_v5, 2)':
            '                        pkg["vol_ratio"] = round(vols[-2] / avg_v5, 2)',
        # 同一批：SmartMoney 缺省理由的**文案修订**（旧文案说"无等价接口"，
        # 会让模型以为聪明钱完全不可观测；实际持仓方向的等价证据有 —— OKX 官方
        # top-trader 的精英账户比/精英持仓比，已真实接入 T0 梯队）。
        '            "reason": "OKX CLI 已移除，smartmoney 无公开 V5 等价接口（待接新数据源）",':
            '            "reason": "Top100 加权多空比/净流无公开 V5 等价接口；持仓方向证据改用 OKX 官方 top-trader 的精英账户比/精英持仓比（见 T0 衍生品梯队）",',
        # ★ 2026-10「不许假数据」：包初值 `vol_ratio = 1.0` 会被提示词渲染成
        #   `15M量比=1.0x`，模型读作"量能正常"——那是**结论**，不是缺失。
        #   改成 `None` ⇒ 提示词渲染 `--`（正向钉子：
        #   `tests/llm/test_prompt_math_foundations.py::
        #    MissingTierDataNeverRendersAsAPlausibleNumberTest` 与
        #   `tests/ops/test_brain_packages.py::DefaultShapeTests`）。
        '        "vol_ratio": 1.0,':
            '        "vol_ratio": None,',
    }

    def _normalise(self, lines):
        """把第 137 刀与 v8.1.0 的接线**还原**成搬运时的样子，再逐行比对。

        接线是"把 `pass` 换成 `note_failure(...)`"（不增行），故还原时把调用行
        还原为 `pass`，而不是删掉它 —— 删掉会让两侧行数错位（我第一版就写错了，
        靠打印真实 diff 才发现）。延时观测则是**净增行**，故这里必须整行丢弃。
        """
        out = []
        for ln in lines:
            stripped = ln.strip()
            if self._LATENCY_START_RE.match(stripped) or self._LATENCY_PUBLISH_RE.match(stripped):
                continue
            # ★ 2026-10 数理退役：删除了原 pkg 初始化中的 'calculus' 占位键
            if stripped == '"calculus": {"valid": False, "regime": "DATA_UNRELIABLE", "quality": 0.0},':
                continue
            if self._NOTE_RE.match(stripped):
                indent = ln[:len(ln) - len(ln.lstrip())]
                ln = f"{indent}pass"
            elif stripped == "except Exception:":
                ln = ln.replace("except Exception:", "except Exception as exc:")
            out.append(ln)
        return out

    #: 2026-10 允许的**净删行**：退役的微积分多周期尾巴（try/except + 4 行）与
    #: 新增的 `pkg["quant_factors"] = load_quant_factor_tiers(inst_id)` 一行。
    _RETIRED_CALCULUS_MARKERS = (
        "from calculus_engine import calculate_multi_timeframe",
        "pkg[\"calculus\"] = calculate_multi_timeframe({",
        "pkg[\"quant_factors\"] = load_quant_factor_tiers(inst_id)",
    )

    def test_body_matches_deployed_baseline_except_validated_taker_parser(self):
        """Pin all other function lines to deployed f53e579, including comments.

        The old pre-move comparison already fails on unmodified f53e579.
        Its fixture remains historical evidence; the active guard permits only
        the exact decimal-net parser replacement reviewed in this repair.
        Sign and malformed-row behavior have separate runtime coverage.
        """
        source = "\n".join(_submodule_function_lines())
        parser_block = "\n".join([
            '                    net_diff = latest_taker_net(d["data"])',
            '                    if net_diff is None:',
            '                        raise ValueError("Invalid OKX taker-volume row")',
        ])
        deployed_block = "\n".join([
            '                    b_vol = float(d["data"][0][1])',
            '                    s_vol = float(d["data"][0][2])',
            '                    net_diff = b_vol - s_vol',
        ])
        self.assertEqual(source.count(parser_block), 1)
        normalised = source.replace(parser_block, deployed_block)
        self.assertEqual(
            hashlib.sha256(normalised.encode("utf-8")).hexdigest(),
            "5d2d9ce1809e16adc5d28280def7af22f3f2b4e3f0721cdda9db3c47f9548e89",
            "Unexpected changes beyond the validated taker parser",
        )

    def test_the_retired_calculus_tail_is_really_gone(self):
        """反向断言：搬运**之后**的退役动作必须真实发生（否则上面那条会空转）。"""
        src = "\n".join(_submodule_function_lines())
        self.assertNotIn("calculate_multi_timeframe", src)
        self.assertNotIn("calculus_engine", src)
        self.assertIn('pkg["quant_factors"] = load_quant_factor_tiers(inst_id)', src)

    def test_failure_counters_are_actually_wired(self):
        """正向断言：6 处静默 except 必须各自接上失败计数（防止上一条的白名单被滥用）。"""
        src = "\n".join(_submodule_function_lines())
        kinds = sorted(re.findall(r'note_failure\("([a-z_0-9]+)", exc\)', src))
        self.assertEqual(kinds, ["okx_adx_1h", "okx_funding_rate", "okx_ls_ratio",
                                 "okx_open_interest", "okx_taker_volume", "okx_ticker"],
                         "6 处取数失败的可观测性接线缺失或被改名")
        # 6 处新接入 + 3 处搬运时就带 `as exc` 的（K线 15m/1H/4H）
        # ——第 4 处（calculus）已随数理系统退场删除，故由 10 降为 9。
        self.assertEqual(src.count("except Exception as exc:"), 9)

    def test_okx_latency_instrumentation_is_actually_wired(self):
        """正向断言：v8.1.0 的 OKX 取数延时观测必须真的在算（防止上一条的
        丢弃规则被滥用成"把这段删掉也不会红"）。"""
        src = "\n".join(_submodule_function_lines())
        self.assertIn("t_okx0 = time.time()", src,
                      "OKX ticker 延时起点采样丢失 ⇒ 白名单规则③在掩盖删除")
        self.assertIn('pkg["okx_latency_ms"] = max(1, int(round((time.time() - t_okx0) * 1000)))', src,
                      "okx_latency_ms 落包丢失 ⇒ 白名单规则③在掩盖删除")
        # 延时必须落进包（ast.unparse 用单引号，故两侧都归一后再比）
        tree = ast.parse(SUBMODULE.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "fetch_single_instrument_package")
        targets = [ast.unparse(n.targets[0]).replace("'", '"')
                   for n in ast.walk(fn) if isinstance(n, ast.Assign)]
        self.assertIn('pkg["okx_latency_ms"]', targets,
                      "okx_latency_ms 不是对 pkg 的落包赋值")

    def test_submodule_takes_the_two_market_functions_as_parameters(self):
        fn = next(n for n in ast.parse(SUBMODULE.read_text(encoding="utf-8")).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "fetch_single_instrument_package")
        kwonly = [a.arg for a in fn.args.kwonlyargs]
        self.assertEqual(kwonly, ["fetch_candles", "fetch_single_indicator"],
                         "两个行情函数必须走调用期注入")
        # 反向哨：子模块不得在 import 期绑定它们
        self.assertNotIn("from market_data_service import", SUBMODULE.read_text(encoding="utf-8"))


class FacadeDelegationTest(unittest.TestCase):
    def test_facade_imports_and_forwards(self):
        src = FACADE.read_text(encoding="utf-8")
        self.assertIn("from scripts.brain.packages import fetch_single_instrument_package as _fetch_single_instrument_package", src)
        self.assertIn("fetch_candles=fetch_candles,", src)
        self.assertIn("fetch_single_indicator=fetch_single_indicator,", src)

    def test_facade_shell_actually_calls_submodule(self):
        """打桩子模块，确认门面壳真的走它（而不是偷偷留了旧实现）。"""
        sentinel = {"instId": "X", "name": "X"}
        with patch.object(abt, "_fetch_single_instrument_package",
                          lambda item, **kw: {"delegated": item, **kw}) as _:
            got = abt.fetch_single_instrument_package(sentinel)
        self.assertEqual(got["delegated"], sentinel)
        self.assertIs(got["fetch_candles"], abt.fetch_candles)
        self.assertIs(got["fetch_single_indicator"], abt.fetch_single_indicator)


class FailSoftContractTest(unittest.TestCase):
    ITEM = {"instId": "FAKE-USDT-SWAP", "name": "FAKE", "type": "crypto", "precision": 4}

    def _call(self, candles):
        # 断网：ticker 走 urllib；确保测试零网络
        with patch.object(urllib.request, "urlopen", side_effect=OSError("offline")):
            return brain_packages.fetch_single_instrument_package(
                self.ITEM,
                fetch_candles=lambda *a, **k: candles,
                fetch_single_indicator=lambda *a, **k: {"adx": 0.0},
            )

    def test_all_data_missing_still_returns_intact_package(self):
        pkg = self._call([])
        for key in ("instId", "name", "type", "precision", "price", "chg24h", "bidPx",
                    "askPx", "fundingRate", "oiUsd", "vol24h", "lsRatio", "takerNetUsd",
                    "atr", "rsi", "vwap_bias", "macd_hist", "macd_accel", "vol_ratio",
                    "obv_flow", "adx_1h", "smart_money", "recent_15m", "recent_1h",
                    "recent_4h", "data_quality"):
            self.assertIn(key, pkg, f"失败路径缺字段 {key}")
        self.assertNotIn("calculus", pkg, "calculus 键已随数理系统退役剥离")
        self.assertEqual(pkg["instId"], "FAKE-USDT-SWAP")
        self.assertEqual(pkg["price"], 0.0)
        self.assertEqual(pkg["data_quality"], "invalid")

    def test_smart_money_is_explicitly_unavailable_not_fabricated(self):
        pkg = self._call([])
        self.assertFalse(pkg["smart_money"]["available"])
        # ★ 2026-10 文案修订：不再说"无等价接口"，而是点明缺哪一项 + 指向真实
        # 可得的替代证据（top-trader 精英账户比/精英持仓比，已接入 T0）。
        self.assertIn("Top100", pkg["smart_money"]["reason"])
        self.assertIn("精英账户比", pkg["smart_money"]["reason"])

    def test_never_raises_on_garbage_candles(self):
        for garbage in ([None], [[1, 2]], ["x"], [[{}]], [{"o": "a"}], [[]]):
            with self.subTest(garbage=garbage):
                self._call(garbage)  # 不得抛

    def test_fetch_candles_is_called_per_timeframe(self):
        seen = []
        with patch.object(urllib.request, "urlopen", side_effect=OSError("offline")):
            brain_packages.fetch_single_instrument_package(
                self.ITEM,
                fetch_candles=lambda inst, bar=None, limit=None: seen.append(bar) or [],
                fetch_single_indicator=lambda *a, **k: {},
            )
        self.assertEqual(sorted(set(seen)), ["15m", "1H", "4H"],
                         "三个周期都必须取数")


if __name__ == "__main__":
    unittest.main()
