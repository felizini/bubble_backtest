#!/usr/bin/env python3
"""Backtester da estratégia cripto "Bolha de Volume".

A estratégia trata um pump de volume como uma bolha de sabão: quando o volume
relativo dispara junto com uma vela de alta, a bolha começa a inflar; quando o
volume perde força ou o preço reverte, a bolha está esvaziando/estourando.

Sem lookahead: todos os sinais são avaliados no fechamento de um candle já
concluído e executados na abertura do candle seguinte.
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

Bar = Dict[str, Any]
Trade = Dict[str, Any]


@dataclass(frozen=True)
class BubbleConfig:
    """Parâmetros da estratégia e da simulação."""

    vol_lookback: int = 10
    vol_spike_mult: float = 3.0
    avoid_recent_red_spike_bars: int = 2
    red_spike_mult: float = 3.0
    require_green_signal: bool = True
    hard_stop_pct: float = 0.04
    trailing_stop_pct: float = 0.05
    vol_decay_ratio: float = 1.3
    max_hold_bars: int = 8
    cooldown_bars: int = 2
    fee_pct: float = 0.001
    capital_inicial: float = 1000.0
    position_size_pct: float = 1.0

    def validate(self) -> None:
        """Valida parâmetros para evitar backtests sem sentido."""
        if self.vol_lookback < 1:
            raise ValueError("vol_lookback deve ser >= 1")
        if self.vol_spike_mult <= 0:
            raise ValueError("vol_spike_mult deve ser > 0")
        if self.avoid_recent_red_spike_bars < 0:
            raise ValueError("avoid_recent_red_spike_bars deve ser >= 0")
        if self.red_spike_mult <= 0:
            raise ValueError("red_spike_mult deve ser > 0")
        if not 0 <= self.hard_stop_pct < 1:
            raise ValueError("hard_stop_pct deve estar em [0, 1)")
        if not 0 <= self.trailing_stop_pct < 1:
            raise ValueError("trailing_stop_pct deve estar em [0, 1)")
        if self.vol_decay_ratio <= 0:
            raise ValueError("vol_decay_ratio deve ser > 0")
        if self.max_hold_bars < 1:
            raise ValueError("max_hold_bars deve ser >= 1")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars deve ser >= 0")
        if not 0 <= self.fee_pct < 1:
            raise ValueError("fee_pct deve estar em [0, 1)")
        if self.capital_inicial <= 0:
            raise ValueError("capital_inicial deve ser > 0")
        if not 0 < self.position_size_pct <= 1:
            raise ValueError("position_size_pct deve estar em (0, 1]")


def parse_time(value: str) -> datetime:
    """Converte horários ISO comuns de CSV OHLCV para datetime."""
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def load_csv(path: str | Path) -> List[Bar]:
    """Carrega um CSV OHLCV como lista de dicionários."""
    with Path(path).open(newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def prepare_data(rows: List[Bar], cfg: BubbleConfig, time_col: str) -> List[Bar]:
    """Prepara dados e indicadores sem usar informação futura."""
    required = {time_col, "open", "high", "low", "close", "volume"}
    if not rows:
        raise ValueError("CSV vazio")
    missing = sorted(required.difference(rows[0].keys()))
    if missing:
        raise ValueError(f"CSV sem colunas obrigatórias: {', '.join(missing)}")

    bars: List[Bar] = []
    for row in rows:
        bar = dict(row)
        bar[time_col] = parse_time(str(row[time_col]))
        for col in ["open", "high", "low", "close", "volume"]:
            bar[col] = float(row[col])
        bars.append(bar)

    bars.sort(key=lambda item: item[time_col])
    volumes: List[float] = []
    for bar in bars:
        if len(volumes) >= cfg.vol_lookback:
            base = sum(volumes[-cfg.vol_lookback :]) / cfg.vol_lookback
            bar["vol_ma"] = base
            bar["vol_ratio"] = bar["volume"] / base if base else math.inf
        else:
            bar["vol_ma"] = None
            bar["vol_ratio"] = None
        bar["is_green"] = bar["close"] > bar["open"]
        volumes.append(bar["volume"])
    return bars


def entry_signal(signal_bar: Bar, cfg: BubbleConfig) -> bool:
    """Retorna True quando uma bolha de volume começa a inflar."""
    vol_ratio = signal_bar.get("vol_ratio")
    if vol_ratio is None:
        return False
    has_volume_spike = vol_ratio >= cfg.vol_spike_mult
    has_required_color = (not cfg.require_green_signal) or bool(signal_bar["is_green"])
    return bool(has_volume_spike and has_required_color)


def has_recent_red_volume_spike(bars: List[Bar], signal_idx: int, cfg: BubbleConfig) -> bool:
    """Bloqueia entrada após spike de volume em candle vermelho recente."""
    if cfg.avoid_recent_red_spike_bars == 0:
        return False

    start_idx = max(0, signal_idx - cfg.avoid_recent_red_spike_bars)
    for bar in bars[start_idx:signal_idx]:
        vol_ratio = bar.get("vol_ratio")
        if vol_ratio is None:
            continue
        is_red = bar["close"] < bar["open"]
        if is_red and vol_ratio >= cfg.red_spike_mult:
            return True
    return False


def exit_signal(
    signal_bar: Bar,
    entry_price: float,
    peak_close: float,
    bars_held: int,
    cfg: BubbleConfig,
) -> Optional[str]:
    """Retorna o motivo de saída usando apenas o candle fechado mais recente."""
    if signal_bar["close"] <= entry_price * (1 - cfg.hard_stop_pct):
        return "hard_stop"
    if signal_bar["close"] <= peak_close * (1 - cfg.trailing_stop_pct):
        return "trailing_stop"
    vol_ratio = signal_bar.get("vol_ratio")
    if vol_ratio is not None and vol_ratio < cfg.vol_decay_ratio:
        return "volume_decay"
    if bars_held >= cfg.max_hold_bars:
        return "max_hold"
    return None


class BubbleBacktester:
    """Motor de backtest com execução no candle seguinte ao sinal."""

    def __init__(self, cfg: BubbleConfig):
        cfg.validate()
        self.cfg = cfg
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.open_position: Optional[Dict[str, Any]] = None
        self.final_capital = cfg.capital_inicial
        self.data: List[Bar] = []

    def run(self, rows: List[Bar], time_col: str = "open_time_brasilia") -> List[Trade]:
        """Executa o backtest e retorna as operações fechadas."""
        self.trades = []
        self.equity_curve = []
        self.open_position = None

        cfg = self.cfg
        bars = prepare_data(rows, cfg, time_col=time_col)
        capital = cfg.capital_inicial
        in_pos = False
        entry_idx: Optional[int] = None
        entry_price: Optional[float] = None
        peak_close: Optional[float] = None
        cooldown_until = -1

        for i in range(1, len(bars)):
            prev = bars[i - 1]
            row = bars[i]

            if not in_pos:
                if i <= cooldown_until:
                    continue
                signal_idx = i - 1
                if entry_signal(prev, cfg) and not has_recent_red_volume_spike(bars, signal_idx, cfg):
                    in_pos = True
                    entry_idx = i
                    entry_price = row["open"]
                    peak_close = entry_price
                continue

            assert entry_idx is not None and entry_price is not None and peak_close is not None
            bars_held = i - entry_idx - 1
            if i > entry_idx:
                peak_close = max(peak_close, prev["close"])

            reason = exit_signal(prev, entry_price, peak_close, bars_held, cfg)
            if reason is None:
                continue

            exit_price = row["open"]
            alloc = capital * cfg.position_size_pct
            gross_ret = exit_price / entry_price - 1
            net_ret = (1 - cfg.fee_pct) * (exit_price / entry_price) * (1 - cfg.fee_pct) - 1
            pnl = alloc * net_ret
            capital += pnl

            self.trades.append(
                dict(
                    signal_time=bars[entry_idx - 1][time_col],
                    signal_close=bars[entry_idx - 1]["close"],
                    signal_volume=bars[entry_idx - 1]["volume"],
                    signal_vol_ma=bars[entry_idx - 1]["vol_ma"],
                    signal_vol_ratio=bars[entry_idx - 1]["vol_ratio"],
                    entry_time=bars[entry_idx][time_col],
                    exit_time=row[time_col],
                    entry_price=entry_price,
                    exit_price=exit_price,
                    bars_held=bars_held,
                    gross_ret_pct=gross_ret * 100,
                    net_ret_pct=net_ret * 100,
                    pnl=pnl,
                    capital_after=capital,
                    exit_reason=reason,
                    entry_idx=entry_idx,
                    exit_idx=i,
                )
            )
            self.equity_curve.append(dict(time=row[time_col], capital=capital))
            in_pos = False
            cooldown_until = i + cfg.cooldown_bars

        if in_pos:
            assert entry_idx is not None and entry_price is not None
            last = bars[-1]
            self.open_position = dict(
                entry_time=bars[entry_idx][time_col],
                entry_price=entry_price,
                last_price=last["close"],
                last_time=last[time_col],
                unrealized_ret_pct=(last["close"] / entry_price - 1) * 100,
                bars_held=len(bars) - 1 - entry_idx,
            )

        self.final_capital = capital
        self.data = bars
        return self.trades

    def summary(self) -> Dict[str, Any]:
        """Calcula métricas agregadas das operações fechadas."""
        n_trades = len(self.trades)
        wins = sum(1 for trade in self.trades if trade["pnl"] > 0)
        win_rate = wins / n_trades * 100 if n_trades else 0.0
        total_ret_pct = (self.final_capital / self.cfg.capital_inicial - 1) * 100

        if n_trades:
            equity = [self.cfg.capital_inicial] + [trade["capital_after"] for trade in self.trades]
            running_max = equity[0]
            drawdowns: List[float] = []
            for value in equity:
                running_max = max(running_max, value)
                drawdowns.append((value - running_max) / running_max * 100)
            gains = sum(trade["pnl"] for trade in self.trades if trade["pnl"] > 0)
            losses = -sum(trade["pnl"] for trade in self.trades if trade["pnl"] < 0)
            avg_trade = sum(trade["net_ret_pct"] for trade in self.trades) / n_trades
            max_dd = min(drawdowns)
            profit_factor = gains / losses if losses > 0 else math.inf
        else:
            avg_trade = 0.0
            max_dd = 0.0
            profit_factor = math.nan

        return dict(
            n_trades=n_trades,
            win_rate_pct=round(win_rate, 2),
            total_return_pct=round(total_ret_pct, 2),
            final_capital=round(self.final_capital, 2),
            avg_trade_pct=round(avg_trade, 2),
            max_drawdown_pct=round(max_dd, 2),
            profit_factor=round(profit_factor, 2) if math.isfinite(profit_factor) else profit_factor,
            open_position=self.open_position,
        )


def parse_args() -> argparse.Namespace:
    """Lê parâmetros de linha de comando."""
    parser = argparse.ArgumentParser(description="Backtester da estratégia cripto Bolha de Volume")
    parser.add_argument("--csv", default="AXSUSDT_2026-06-19_2026-06-20_1h.csv", help="CSV OHLCV de entrada")
    parser.add_argument("--time-col", default="open_time_brasilia", help="Coluna de tempo de abertura do candle")
    parser.add_argument("--output", default="bubble_backtest_trades.csv", help="CSV de saída com trades fechados")
    parser.add_argument("--vol-lookback", type=int, default=10)
    parser.add_argument("--vol-spike-mult", type=float, default=3.0)
    parser.add_argument(
        "--avoid-recent-red-spike-bars",
        type=int,
        default=2,
        help="Bloqueia entrada se houve spike de volume em candle vermelho nos N candles anteriores; use 0 para desativar",
    )
    parser.add_argument(
        "--red-spike-mult",
        type=float,
        default=3.0,
        help="Múltiplo mínimo de volume relativo para classificar um candle vermelho recente como spike de risco",
    )
    parser.add_argument("--allow-red-signal", action="store_true", help="Não exige candle de alta para entrada")
    parser.add_argument("--hard-stop-pct", type=float, default=0.04)
    parser.add_argument("--trailing-stop-pct", type=float, default=0.05)
    parser.add_argument("--vol-decay-ratio", type=float, default=1.3)
    parser.add_argument("--max-hold-bars", type=int, default=8)
    parser.add_argument("--cooldown-bars", type=int, default=2)
    parser.add_argument("--fee-pct", type=float, default=0.001)
    parser.add_argument("--capital-inicial", type=float, default=1000.0)
    parser.add_argument("--position-size-pct", type=float, default=1.0)
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Mostra contexto candle a candle para explicar trades vencedores/perdedores",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> BubbleConfig:
    """Converte argumentos em configuração de backtest."""
    return BubbleConfig(
        vol_lookback=args.vol_lookback,
        vol_spike_mult=args.vol_spike_mult,
        avoid_recent_red_spike_bars=args.avoid_recent_red_spike_bars,
        red_spike_mult=args.red_spike_mult,
        require_green_signal=not args.allow_red_signal,
        hard_stop_pct=args.hard_stop_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        vol_decay_ratio=args.vol_decay_ratio,
        max_hold_bars=args.max_hold_bars,
        cooldown_bars=args.cooldown_bars,
        fee_pct=args.fee_pct,
        capital_inicial=args.capital_inicial,
        position_size_pct=args.position_size_pct,
    )


def format_value(value: Any) -> str:
    """Formata valores para o relatório textual."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def print_report(bt: BubbleBacktester, trades: List[Trade]) -> None:
    """Imprime operações e resumo no terminal."""
    print("=" * 80)
    print("OPERAÇÕES (TRADES)")
    print("=" * 80)
    if trades:
        cols = [
            "signal_time",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "bars_held",
            "gross_ret_pct",
            "net_ret_pct",
            "signal_vol_ratio",
            "exit_reason",
        ]
        print(" | ".join(cols))
        for trade in trades:
            print(" | ".join(format_value(trade[col]) for col in cols))
    else:
        print("Nenhuma operação foi fechada no período.")

    print("\n" + "=" * 80)
    print("RESUMO")
    print("=" * 80)
    for key, value in bt.summary().items():
        print(f"{key}: {value}")


def print_diagnostics(bt: BubbleBacktester, time_col: str) -> None:
    """Imprime o contexto dos candles que explicam cada operação."""
    if not bt.trades:
        return

    print("\n" + "=" * 80)
    print("DIAGNÓSTICO DOS TRADES")
    print("=" * 80)
    for number, trade in enumerate(bt.trades, start=1):
        entry_idx = int(trade["entry_idx"])
        exit_idx = int(trade["exit_idx"])
        signal_bar = bt.data[entry_idx - 1]
        exit_signal_bar = bt.data[exit_idx - 1]
        print(f"Trade {number}: {trade['net_ret_pct']:.2f}% líquido, saída por {trade['exit_reason']}")
        print(
            "  Sinal: "
            f"{format_value(signal_bar[time_col])}, "
            f"close={signal_bar['close']:.4f}, "
            f"volume={signal_bar['volume']:.2f}, "
            f"vol_ratio={signal_bar['vol_ratio']:.2f}"
        )
        print(
            "  Execução: "
            f"entrada={format_value(trade['entry_time'])} @ {trade['entry_price']:.4f}; "
            f"saída={format_value(trade['exit_time'])} @ {trade['exit_price']:.4f}"
        )
        print(
            "  Candle que disparou a saída: "
            f"{format_value(exit_signal_bar[time_col])}, "
            f"close={exit_signal_bar['close']:.4f}, "
            f"volume={exit_signal_bar['volume']:.2f}, "
            f"vol_ratio={exit_signal_bar['vol_ratio']:.2f}"
        )


def save_trades(path: str | Path, trades: List[Trade]) -> None:
    """Salva operações fechadas em CSV para auditoria."""
    if not trades:
        return
    fieldnames = list(trades[0].keys())
    with Path(path).open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            writer.writerow({key: format_value(value) for key, value in trade.items()})


def main() -> None:
    """Ponto de entrada do CLI."""
    args = parse_args()
    cfg = build_config(args)
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV não encontrado: {csv_path}")
    rows = load_csv(csv_path)
    bt = BubbleBacktester(cfg)
    trades = bt.run(rows, time_col=args.time_col)
    print_report(bt, trades)
    if args.diagnose:
        print_diagnostics(bt, args.time_col)

    if trades:
        save_trades(args.output, trades)
        print(f"\nLog de operações salvo em: {Path(args.output)}")


if __name__ == "__main__":
    main()
