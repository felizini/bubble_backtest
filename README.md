# Estratégia "Bolha de Volume" — Backtester cripto

Este repositório contém um backtester Python para uma estratégia de trading cripto inspirada na analogia de uma **bolha de sabão**:

- Volume de entrada crescente infla a bolha e costuma acompanhar movimentos de pump.
- Depois do volume máximo, a bolha pode estourar rapidamente ou esvaziar de forma gradual.
- A estratégia tenta entrar no início da inflação e sair antes do esvaziamento usando stop fixo, trailing stop, decaimento de volume e tempo máximo em posição.

O script foi calibrado para o evento real de `AXSUSDT` entre `2026-06-19` e `2026-06-20`, disponível em `AXSUSDT_2026-06-19_2026-06-20_1h.csv`.

## Sem lookahead

A simulação evita olhar o futuro:

1. Sinais de entrada e saída são avaliados apenas no **fechamento** do candle concluído.
2. A ordem correspondente é executada na **abertura** do candle seguinte.

Esse fluxo aproxima o comportamento de um bot real, que só conhece o candle depois de ele fechar.

## Instalação

O backtester usa apenas a biblioteca padrão do Python; não há dependências externas.

## Uso rápido

```bash
python bubble_backtest.py
```

Por padrão, o script usa o CSV de AXSUSDT incluído no repositório e grava o log de operações em `bubble_backtest_trades.csv` quando houver trades fechados.

## Exemplo com parâmetros

```bash
python bubble_backtest.py \
  --csv AXSUSDT_2026-06-19_2026-06-20_1h.csv \
  --vol-lookback 10 \
  --vol-spike-mult 3.0 \
  --avoid-recent-red-spike-bars 2 \
  --red-spike-mult 3.0 \
  --hard-stop-pct 0.04 \
  --trailing-stop-pct 0.05 \
  --vol-decay-ratio 1.3 \
  --max-hold-bars 8 \
  --cooldown-bars 2 \
  --fee-pct 0.001 \
  --capital-inicial 1000 \
  --position-size-pct 1.0
```

Use `--allow-red-signal` para permitir entradas em candles de sinal vermelhos. Sem essa opção, a entrada exige candle de alta (`close > open`).

Por padrão, o script também bloqueia novas compras quando houve spike de volume em candle vermelho nos 2 candles anteriores ao sinal. Esse filtro tenta evitar repiques comprados logo após uma liquidação de alto volume, como no evento perdedor de janeiro. Use `--avoid-recent-red-spike-bars 0` para desativar o filtro ou ajuste `--red-spike-mult` para mudar o múltiplo mínimo de volume relativo considerado spike vermelho.

Use `--diagnose` para imprimir, além do relatório padrão, os candles de sinal e de saída de cada operação. Essa opção ajuda a explicar por que um evento foi vencedor ou perdedor.

## Colunas esperadas no CSV

O arquivo de entrada deve conter pelo menos:

- `open_time_brasilia` — horário de abertura do candle, ou outra coluna informada via `--time-col`.
- `open`, `high`, `low`, `close` — OHLC.
- `volume` — volume negociado do candle.

## Saídas do relatório

O relatório impresso no terminal inclui:

- Operações fechadas, com horário de entrada/saída, preços, retorno bruto/líquido e motivo da saída.
- Resumo com número de trades, win rate, retorno total, capital final, drawdown máximo, profit factor e eventual posição ainda aberta marcada a mercado.

## Aviso

Este projeto é apenas educacional e não constitui recomendação financeira. Resultados passados e calibração em um evento específico não garantem desempenho futuro.
