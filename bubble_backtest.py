#!/usr/bin/env python3
"""
================================================================================
 ESTRATEGIA "BOLHA DE VOLUME" — Backtester
================================================================================
Analogia: o ativo se comporta como uma bolha de sabao.
  - Volume de entrada crescente  -> a bolha "infla" (e o preco tende a subir
    junto, em movimentos de pump impulsionados por volume anormal).
  - Ao atingir o volume maximo, a bolha pode:
        (a) estourar de repente (reversao violenta, vela vermelha forte) ou
        (b) esvaziar aos poucos (volume voltando gradualmente ao normal,
            preco perdendo forca).
  - A estrategia tenta ENTRAR quando a bolha comeca a inflar (pico de volume
    relativo + vela de alta) e SAIR antes que ela esvazie, usando uma
    combinacao de: stop-loss fixo, stop movel (trailing) sobre o fechamento,
    deteccao de "esvaziamento" de volume e um limite maximo de tempo em
    posicao.

Esse script foi calibrado e validado em cima do evento real do par
AXSUSDT entre 2026-06-19 e 2026-06-20 (arquivo
AXSUSDT_2026-06-19_2026-06-20_1h.csv), onde duas "bolhas" de volume
ocorreram (~21h do dia 19 e ~21h do dia 20). Os parametros padrao abaixo
reproduzem esse evento: o ciclo 1 e capturado quase por inteiro
(~+23% liquido em 5 candles) e o ciclo 2 demonstra o controle de risco da
estrategia (posicao protegida por stop, ainda aberta no fim dos dados).

IMPORTANTE SOBRE LOOKAHEAD / EXECUCAO REALISTA:
  - Sinais (entrada, stop, trailing, decaimento de volume) sao sempre
    avaliados no FECHAMENTO do candle ja concluido.
  - A ordem (entrada/saida) e executada na ABERTURA do candle seguinte.
  Isso evita "olhar o futuro" dentro do proprio candle de sinal e e o
  comportamento que um bot real teria (ele so sabe que um candle fechou
  depois que ele fecha).
================================================================================
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Dict, Any


# ------------------------------------------------------------------------
# 1) CONFIGURACAO DA ESTRATEGIA
# ------------------------------------------------------------------------
@dataclass
class BubbleConfig:
    # --- Deteccao de "inflacao" (entrada) ---
    vol_lookback: int = 10          # janela (em candles) da media de volume "base"
    vol_spike_mult: float = 3.0     # volume do candle de sinal >= X * media -> bolha comecando a inflar
    require_green_signal: bool = True  # exige candle de sinal de alta (close > open)

    # --- Saida / protecao contra o "estouro" ---
    hard_stop_pct: float = 0.04     # stop fixo: -4% a partir do preco de entrada
    trailing_stop_pct: float = 0.05 # stop movel: -5% a partir do maior fechamento atingido
    vol_decay_ratio: float = 1.3    # se o volume do candle cair abaixo de 1.3x a media -> bolha "esvaziando", sair
    max_hold_bars: int = 8          # tempo maximo em posicao (em candles/horas)

    # --- Gestao de risco / operacional ---
    cooldown_bars: int = 2          # candles de espera apos sair de uma operacao antes de procurar nova entrada
    fee_pct: float = 0.001          # taxa por lado (ex.: 0.10% Binance spot)
    capital_inicial: float = 1000.0
    position_size_pct: float = 1.0  # fracao do capital alocada por operacao (1.0 = 100%)


# ------------------------------------------------------------------------
# 2) PREPARACAO DOS DADOS / INDICADORES
# ------------------------------------------------------------------------
def prepare_data(df: pd.DataFrame, cfg: BubbleConfig,
                  time_col: str = "open_time_brasilia") -> pd.DataFrame:
    """Calcula os indicadores usados pela estrategia.

    Espera um DataFrame com colunas: open, high, low, close, volume
    (nomes iguais aos exportados pela Binance) e uma coluna de tempo.
    """
    d = df.copy()
    d[time_col] = pd.to_datetime(d[time_col])
    d = d.sort_values(time_col).reset_index(drop=True)

    # Media de volume "base" das N velas ANTERIORES (shift(1) evita lookahead:
    # o candle atual nunca participa do calculo da sua propria media de base).
    d["vol_ma"] = d["volume"].rolling(cfg.vol_lookback).mean().shift(1)
    d["vol_ratio"] = d["volume"] / d["vol_ma"]          # "indice de inflacao" da bolha
    d["is_green"] = d["close"] > d["open"]
    return d


# ------------------------------------------------------------------------
# 3) FUNCAO DE SINAL (reutilizavel tambem por um bot ao vivo)
# ------------------------------------------------------------------------
def entry_signal(signal_bar: pd.Series, cfg: BubbleConfig) -> bool:
    """Avalia, a partir de um candle ja FECHADO, se a bolha comecou a inflar.

    Em um bot ao vivo, basta chamar essa funcao a cada novo candle fechado,
    passando a ultima linha do dataframe (apos prepare_data ser reaplicado
    de forma incremental).
    """
    if pd.isna(signal_bar["vol_ratio"]):
        return False
    cond = signal_bar["vol_ratio"] >= cfg.vol_spike_mult
    if cfg.require_green_signal:
        cond = cond and bool(signal_bar["is_green"])
    return bool(cond)


def exit_signal(signal_bar: pd.Series, entry_price: float, peak_close: float,
                 bars_held: int, cfg: BubbleConfig) -> Optional[str]:
    """Avalia, a partir do ultimo candle FECHADO, se devemos sair da posicao.
    Retorna o motivo da saida ou None se deve continuar segurando.
    """
    if signal_bar["close"] <= entry_price * (1 - cfg.hard_stop_pct):
        return "hard_stop"
    if signal_bar["close"] <= peak_close * (1 - cfg.trailing_stop_pct):
        return "trailing_stop"
    if pd.notna(signal_bar["vol_ratio"]) and signal_bar["vol_ratio"] < cfg.vol_decay_ratio:
        return "volume_decay"
    if bars_held >= cfg.max_hold_bars:
        return "max_hold"
    return None


# ------------------------------------------------------------------------
# 4) MOTOR DE BACKTEST
# ------------------------------------------------------------------------
class BubbleBacktester:
    def __init__(self, cfg: BubbleConfig):
        self.cfg = cfg
        self.trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.open_position: Optional[Dict[str, Any]] = None

    def run(self, df: pd.DataFrame, time_col: str = "open_time_brasilia") -> pd.DataFrame:
        cfg = self.cfg
        d = prepare_data(df, cfg, time_col=time_col)

        capital = cfg.capital_inicial
        in_pos = False
        entry_idx = entry_price = peak_close = None
        cooldown_until = -1
        n = len(d)

        for i in range(1, n):
            prev = d.iloc[i - 1]   # ultimo candle FECHADO (onde o sinal e avaliado)
            row = d.iloc[i]        # candle de execucao (ordem entra/sai na abertura dele)

            if not in_pos:
                if i <= cooldown_until:
                    continue
                if entry_signal(prev, cfg):
                    in_pos = True
                    entry_idx = i
                    entry_price = row["open"]
                    peak_close = entry_price
            else:
                bars_held = i - entry_idx - 1
                sig = prev
                if i > entry_idx:
                    peak_close = max(peak_close, sig["close"])

                reason = exit_signal(sig, entry_price, peak_close, bars_held, cfg)
                if reason:
                    exit_price = row["open"]
                    alloc = capital * cfg.position_size_pct
                    gross_ret = exit_price / entry_price - 1
                    net_ret = (1 - cfg.fee_pct) * (exit_price / entry_price) * (1 - cfg.fee_pct) - 1
                    pnl = alloc * net_ret
                    capital += pnl

                    self.trades.append(dict(
                        entry_time=d.iloc[entry_idx][time_col],
                        exit_time=row[time_col],
                        entry_price=entry_price,
                        exit_price=exit_price,
                        bars_held=bars_held,
                        gross_ret_pct=gross_ret * 100,
                        net_ret_pct=net_ret * 100,
                        pnl=pnl,
                        capital_after=capital,
                        exit_reason=reason,
                    ))
                    self.equity_curve.append(dict(time=row[time_col], capital=capital))
                    in_pos = False
                    cooldown_until = i + cfg.cooldown_bars

        # posicao ainda aberta ao final dos dados -> marcar a mercado (nao realizado)
        if in_pos:
            last = d.iloc[-1]
            unreal_ret = (last["close"] / entry_price - 1) * 100
            self.open_position = dict(
                entry_time=d.iloc[entry_idx][time_col],
                entry_price=entry_price,
                last_price=last["close"],
                last_time=last[time_col],
                unrealized_ret_pct=unreal_ret,
                bars_held=n - 1 - entry_idx,
            )

        self.final_capital = capital
        self.data = d
        return pd.DataFrame(self.trades)

    # --------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        cfg = self.cfg
        t = pd.DataFrame(self.trades)
        n_trades = len(t)
        wins = (t["pnl"] > 0).sum() if n_trades else 0
        win_rate = (wins / n_trades * 100) if n_trades else 0.0
        total_ret_pct = (self.final_capital / cfg.capital_inicial - 1) * 100

        if n_trades:
            equity = [cfg.capital_inicial] + t["capital_after"].tolist()
            running_max = pd.Series(equity).cummax()
            drawdown = (pd.Series(equity) - running_max) / running_max * 100
            max_dd = drawdown.min()
            avg_trade = t["net_ret_pct"].mean()
            gains = t.loc[t["pnl"] > 0, "pnl"].sum()
            losses = -t.loc[t["pnl"] < 0, "pnl"].sum()
            profit_factor = (gains / losses) if losses > 0 else np.inf
        else:
            max_dd = 0.0
            avg_trade = 0.0
            profit_factor = np.nan

        return dict(
            n_trades=n_trades,
            win_rate_pct=round(win_rate, 2),
            total_return_pct=round(total_ret_pct, 2),
            final_capital=round(self.final_capital, 2),
            avg_trade_pct=round(avg_trade, 2),
            max_drawdown_pct=round(max_dd, 2),
            profit_factor=round(profit_factor, 2) if np.isfinite(profit_factor) else profit_factor,
            open_position=self.open_position,
        )


# ------------------------------------------------------------------------
# 5) EXECUCAO / RELATORIO
# ------------------------------------------------------------------------
if __name__ == "__main__":
    CSV_PATH = "AXSUSDT_2026-06-19_2026-06-20_1h.csv"

    cfg = BubbleConfig(
        vol_lookback=10,
        vol_spike_mult=3.0,
        require_green_signal=True,
        hard_stop_pct=0.04,
        trailing_stop_pct=0.05,
        vol_decay_ratio=1.3,
        max_hold_bars=8,
        cooldown_bars=2,
        fee_pct=0.001,
        capital_inicial=1000.0,
        position_size_pct=1.0,
    )

    df_raw = pd.read_csv(CSV_PATH)
    bt = BubbleBacktester(cfg)
    trades = bt.run(df_raw)

    pd.set_option("display.width", 160)
    print("=" * 80)
    print("OPERACOES (TRADES)")
    print("=" * 80)
    if len(trades):
        cols = ["entry_time", "exit_time", "entry_price", "exit_price",
                "bars_held", "gross_ret_pct", "net_ret_pct", "exit_reason"]
        print(trades[cols].round(4).to_string(index=False))
    else:
        print("Nenhuma operacao foi fechada no periodo.")

    print()
    print("=" * 80)
    print("RESUMO")
    print("=" * 80)
    for k, v in bt.summary().items():
        print(f"{k}: {v}")

    # salva o log de operacoes para auditoria / uso futuro
    if len(trades):
        trades.to_csv("bubble_backtest_trades.csv", index=False)
