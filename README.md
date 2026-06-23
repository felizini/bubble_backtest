================================================================================
 ESTRATEGIA "BOLHA DE VOLUME" — Backtester
================================================================================
Analogia: o ativo se comporta como uma bolha de sabão.
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
