[Read in English](README.md)

# Bot Autonomo de Trading para Polymarket

Um bot de trading autonomo 24/7 para os mercados de previsao do [Polymarket](https://polymarket.com). Desenhado para capital reduzido (<$500 USDC) a correr num VPS Linux.

## O Que Este Bot Faz

O bot executa tres estrategias para encontrar oportunidades lucrativas no Polymarket:

### Estrategia 1: Flash Crash
Monitoriza mercados cripto Up/Down de 15 minutos (BTC, ETH, SOL, XRP) via WebSocket. Quando uma probabilidade cai abruptamente (>28% em 12 segundos), compra o lado que caiu e sai com +8% de take profit ou -4% de stop loss. Estas quedas sao tipicamente causadas por grandes ordens de venda que empurram temporariamente o preco abaixo do valor justo.

### Estrategia 2: Mispricing Entre Mercados
Compara os precos do Polymarket com os mesmos eventos no Kalshi, Metaculus e Manifold Markets. Quando os precos divergem mais de 8 pontos percentuais, negoceia o lado subvalorizado. O pressuposto e que os precos dos mercados de previsao convergem ao longo do tempo.

### Estrategia 3: Agente IA (com Claude)
Para casos ambiguos (divergencia de 5-8%), pede ao Claude AI para analisar o mercado com contexto de noticias recentes. So negoceia quando o Claude esta confiante e concorda com o sinal de preco externo. Limitado a 50 chamadas API/dia para controlar custos.

## Aviso de Risco

**Podes e provavelmente vais perder dinheiro.** Os mercados de previsao sao especulativos e comportam risco significativo. Este bot e software experimental — bugs, alteracoes de API ou condicoes de mercado podem resultar em perda total de capital. Usa apenas dinheiro que possas perder. Resultados passados de backtesting nao garantem resultados futuros.

## Guia de Configuracao

### Pre-requisitos
- Uma carteira MetaMask com USDC na rede Polygon
- Uma conta Polymarket (verifica [polymarket.com/tos](https://polymarket.com/tos) para restricoes geograficas)
- Um VPS (recomendado: Hetzner CX22, ~6 EUR/mes)
- Docker e Docker Compose instalados

### Passo 1: Obter Credenciais do Polymarket
1. Cria uma carteira no [MetaMask](https://metamask.io)
2. Faz bridge de USDC para a rede Polygon (usa [bridge.polygon.technology](https://bridge.polygon.technology))
3. Regista-te em [polymarket.com](https://polymarket.com) e liga a tua carteira
4. Vai a Definicoes e anota o teu Safe Address
5. Candidata-te ao Builder Program em [docs.polymarket.com](https://docs.polymarket.com) para obter chaves API (permite transaccoes sem gas)

### Passo 2: Clonar e Configurar
```bash
git clone https://github.com/YOUR_USERNAME/polymarket_bot.git
cd polymarket_bot
cp .env.example .env
# Edita o .env com as tuas credenciais
```

### Passo 3: Correr o Backtest Primeiro
Antes de arriscar dinheiro real, corre o backtest com dados historicos:
```bash
# Clonar o dataset historico
git clone https://github.com/Jon-Becker/prediction-market-analysis
cd prediction-market-analysis && make setup  # Descarrega ~36GB de dados
cd ../polymarket_bot

pip install -r requirements.txt
python -m scripts.backtest
```
Revisa o resultado. So avanca se o backtest mostrar valor esperado positivo.

### Passo 4: Deploy com Docker
```bash
docker compose up -d
docker compose logs -f  # Ver os logs
```

### Passo 5: Lancamento Gradual
1. **Semana 1-2**: Corre com `DRY_RUN=true` — o bot regista o que negociaria mas nao coloca ordens reais
2. **Semana 3**: Define `DRY_RUN=false`, comeca com $20 USDC apenas no Flash Crash
3. **Semana 5+**: Activa Mispricing apos o Flash Crash estar estavel
4. **Semana 7+**: Activa o Agente IA apos o Mispricing estar estavel

## Dashboard e Logs

### Dashboard no Terminal
Se estiver a correr interactivamente (nao em Docker), o bot mostra um dashboard em tempo real no terminal com posicoes, P&L e estado das estrategias.

### Dashboard Web
Abre `docs/index.html` num browser (ou serve via GitHub Pages). O bot escreve `docs/data/status.json` a cada 60 segundos com o estado actual. O dashboard suporta Ingles e Portugues.

### Alertas Telegram
Configura `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no `.env` para receber alertas sobre:
- Inicio/reinicio do bot
- Novas posicoes abertas
- Posicoes fechadas (com P&L)
- Stop-loss diario activado
- Circuit breaker activado
- Relatorio diario as 08:00 UTC

### Logs
Guardados no directorio `logs/`, rodados diariamente, mantidos durante 30 dias.

## Ajustar Parametros de Risco

Edita no `.env`:

| Parametro | Predefinicao | Descricao |
|-----------|-------------|-----------|
| `DAILY_LOSS_LIMIT_PCT` | 0.05 | Para o trading se a perda diaria exceder 5% do capital |
| `MAX_POSITION_SIZE_PCT` | 0.10 | Maximo 10% do capital por posicao |
| `MAX_TOTAL_EXPOSURE_PCT` | 0.60 | Maximo 60% do capital em posicoes abertas |
| `FLASH_CRASH_DROP_THRESHOLD` | 0.28 | Queda minima de preco para accionar compra flash crash |
| `FLASH_CRASH_SIZE_USDC` | 5.0 | Tamanho por trade de flash crash |
| `MIN_MISPRICING_PCT` | 0.08 | Divergencia minima para accionar trade de mispricing |

## Arquitectura

```
main.py                  Orquestrador — liga tudo
core/                    API Polymarket (bot, websocket, gamma, signer, crypto)
strategies/              Flash Crash, Mispricing, Agente IA
execution/               Executor de ordens em tres fases, ciclo de vida das ordens
data/                    Scanner de mercados, fontes externas, indexador
risk/                    Gestor de risco (stop-loss, circuit breaker, limites)
ai/                      Analista Claude
storage/                 Base de dados SQLite, armazenamento Parquet
notifications/           Alertas Telegram
monitor/                 Dashboard TUI no terminal, escritor de status.json
scripts/                 Backtesting
tests/                   Testes unitarios
docs/                    Dashboard de monitorizacao web (HTML/CSS/JS)
```

## Correr Testes

```bash
pip install -r requirements.txt
pytest tests/ -v --cov
```

## Creditos

Construido e adaptado a partir destes repositorios open-source (licenca MIT):
- [discountry/polymarket-trading-bot](https://github.com/discountry/polymarket-trading-bot) — Infraestrutura core de trading
- [Polymarket/agents](https://github.com/Polymarket/agents) — Padrao de agente IA e dados de mercado
- [Jon-Becker/prediction-market-analysis](https://github.com/Jon-Becker/prediction-market-analysis) — Dados historicos e backtesting
- [zydomus219/Polymarket-betting-bot](https://github.com/zydomus219/Polymarket-betting-bot) — Execucao de ordens em tres fases

## Restricoes Geograficas

O Polymarket pode nao estar disponivel em todas as jurisdicoes. Verifica [polymarket.com/tos](https://polymarket.com/tos) antes de usar este bot. E da tua responsabilidade cumprir as leis e regulamentos locais.

## Licenca

MIT
