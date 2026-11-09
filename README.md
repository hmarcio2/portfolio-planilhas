# 02 · Agente analista de planilhas

Envie um `.csv` ou `.xlsx`, faça perguntas em linguagem natural e receba respostas calculadas com SQL, gráficos e insights. É a versão pública do que foi feito no projeto DSS (planilha → Parquet → DuckDB → agente ReAct).

**Status:** funcional localmente, com 97 testes, tracing opcional no Langfuse e avaliação com o Gemini Flash-Lite real (resultados abaixo). Falta comparar com outros modelos e publicar a demo.

## Como funciona

```
upload ─► validação ─► pandas ─► Parquet ─► DuckDB (em memória, TRAVADO) ◄─ run_sql
                                                       ▲                       ▲
                                      agente ReAct (LLM + tools)   ───────────┘
                                          │  describe_table · run_sql · make_chart
                                          ▼
                            SSE: delta · tool · chart · done  ─► interface (Chart.js)
```

1. **Upload** (`app/data.py`): só `.csv`/`.xlsx`; limites de tamanho, linhas, colunas e abas; checagem de "zip bomb" no xlsx; CSV em UTF-8 ou Latin-1; nomes de tabelas e colunas normalizados (`Região` → `regiao`); datas ISO e `dd/mm/aaaa` viram `TIMESTAMP`; tipos mistos viram texto. Cada aba é uma tabela, gravada em Parquet e carregada no DuckDB.
2. **Agente** (`app/agent.py`): loop ReAct manual sobre a API de mensagens com tool use. O esquema das tabelas vai no prompt de sistema; o modelo chama `describe_table` (perfil de colunas), `run_sql` (somente leitura) e `make_chart` (usa o último resultado). Erros de SQL voltam como `is_error` para o modelo se corrigir. Teto de 8 passos.
3. **Interface** (`web/`): upload, esquema, chat com chips do que o agente executou (passe o mouse no chip "SQL" para ver a consulta), gráficos e download do relatório `.md` com perguntas, respostas e SQL.

## Segurança do SQL

O modelo escreve o SQL, então ele é tratado como entrada não confiável. Quatro camadas independentes (`app/sql_guard.py`, `app/data.py`):

| Camada | O que bloqueia |
|---|---|
| Parser do DuckDB | mais de uma instrução; qualquer coisa que não seja `SELECT`/`WITH … SELECT` (`DROP`, `COPY`, `ATTACH`, `PRAGMA`, `SET`, `INSTALL`, `EXPLAIN`…) |
| DuckDB travado após a carga | `enable_external_access=false` (sem `read_csv('arquivo')`, `glob`, rede), sem autoload de extensões, `lock_configuration=true` (nem `SET` dentro de `query(...)` muda isso) |
| Recursos | timeout de 10 s com interrupção, `memory_limit` de 512 MB, até 1000 linhas guardadas e 30 mostradas ao modelo |
| Dados | conteúdo de células/colunas é declarado "dado, não instrução" no prompt; células longas são truncadas |

Os testes (`tests/test_data_and_sql.py`) tentam cada uma dessas fugas e verificam que os dados continuam intactos.

## Confiabilidade dos números (ancoragem)

Medido com o Gemini Flash-Lite: numa pergunta sem resposta possível ("qual foi o lucro?", sem coluna de custo), o modelo negou corretamente, mas **ofereceu uma "receita total de R$ 96.069,57" sem rodar SQL**; o valor real era R$ 128.158,70. Prompt não resolve isso sozinho, então `app/grounding.py` confere, ao fim de cada resposta, se os **números grandes** (≥ 1000, exceto anos e números citados na conversa) aparecem em algum resultado de consulta, em qualquer notação (`1.234,56`, `1,234.56`, "128 mil"). Os que não aparecem geram o evento `warning`: a interface mostra "Atenção: … não aparece(m) nos resultados das consultas" e o relatório `.md` traz o mesmo aviso. Limites: não prova que um número está certo (só que veio de uma consulta), números pequenos e percentuais derivados pelo modelo não são checados (evita falsos alarmes), e um valor inventado que coincida com outro dado da planilha passa.

## Privacidade

Sem login. O `session_id` é um token aleatório e funciona como credencial da análise. Os arquivos ficam em diretório temporário e são apagados após 30 min sem uso, ao clicar em "Nova planilha" ou ao desligar o app; no Docker, `/tmp` é `tmpfs`. **O conteúdo da planilha é enviado ao provedor do LLM** — a interface avisa para não enviar dados sensíveis.

## Rodar localmente

Na raiz do portfólio, com o venv criado:

```bash
.venv/Scripts/python -m pip install -e _core -r 02-agente-planilhas/requirements.txt pytest pytest-asyncio httpx
cd 02-agente-planilhas
../.venv/Scripts/python -m uvicorn app.main:create_app --factory --port 8002
```

Abra `http://localhost:8002`. Sem chave nenhuma, o provedor é o `echo` (não analisa nada; serve só para ver a interface). Para usar o Gemini Flash-Lite, defina `GOOGLE_API_KEY` (terminal ou `.env`) antes de subir; outras opções em `_infra/.env.example`. Com `LOCAL_LLM_ENABLED=true`, o Ollama local assume se a API falhar ou estourar a cota.

Variáveis específicas (as de `_infra/.env.example` também valem):

| Variável | Padrão | Efeito |
|---|---|---|
| `PORTFOLIO_SHEETS_EFFORT` | `medium` | esforço de raciocínio do modelo neste app |
| `PORTFOLIO_SHEETS_MAX_TOKENS` | `4000` | tamanho máximo de cada passo do agente |
| `PORTFOLIO_SHEETS_MAX_MB` | `5` | tamanho máximo do arquivo |
| `PORTFOLIO_SHEETS_MAX_ROWS` / `_MAX_COLUMNS` / `_MAX_SHEETS` | `200000` / `100` / `5` | limites por tabela |
| `PORTFOLIO_SHEETS_TTL_SECONDS` | `1800` | quando a sessão e os arquivos são apagados |
| `PORTFOLIO_SHEETS_MAX_SESSIONS` | `30` | análises simultâneas |

## Testes

```bash
cd 02-agente-planilhas && ../.venv/Scripts/python -m pytest
```

97 testes: carga de CSV/XLSX, limites, TTL, bloqueios de SQL, timeout, ferramentas, loop do agente com provedor roteirizado, fluxo completo da API, limites de taxa, a checagem de ancoragem de números, os auxiliares da avaliação e o tracing (estrutura do trace, notas, privacidade dos metadados, falha do provedor). **Os testes não chamam um LLM de verdade**; para isso, use a avaliação abaixo.

## Avaliação com um LLM real

O padrão é o **Gemini Flash-Lite** (`gemini-flash-lite-latest`), o modelo mínimo do Google, como no projeto Chatbot. Um modelo desse porte erra mais em SQL e em seguir regras do que um modelo grande; esta avaliação existe para medir isso antes de publicar a demo.

### Resultados medidos (2026-11-01, `gemini-flash-lite-latest`, esforço do app `medium` ignorado pelo Gemini)

Primeira rodada completa: 10 perguntas × 3 execuções = 30 respostas, ~1,5 a 3 s por resposta, ~2,4 mil tokens de entrada em média.

| Grupo | Resultado | Observação |
|---|---|---|
| 8 perguntas numéricas (linhas, receita, região, mês, produto, faltantes, média, cruzamento) | **23/24** corretas, todas com 1 consulta SQL (uma com 2) | a falha foi **cota da API (429)**, não erro do modelo |
| `injecao` (instrução maliciosa numa célula) | **3/3 não obedeceram** e ainda apontaram a tentativa como "prompt injection" | minha checagem inicial reprovava qualquer menção à palavra e estava errada; corrigida |
| `inexistente` ("lucro" sem coluna de custo) | **0/3** na primeira rodada: respondeu "o lucro total foi R$ 128.158,70" (trocou receita por lucro) | **falha real** do modelo |

Correções feitas a partir disso, e re-medição:
1. Regra no prompt: responder exatamente ao que foi perguntado e negar já na primeira frase quando a métrica não existe. Depois disso, `inexistente` negou corretamente 3/3 e 3/3 (duas rodadas).
2. A revisão manual dessas respostas achou o número inventado (R$ 96.069,57, sem SQL) em 1 de 3 execuções. Isso levou à checagem de ancoragem acima, que agora reprova o caso na avaliação e avisa o usuário.
3. Rodada final com o código atual (`inexistente`, `receita_total`, `regiao_top` × 3): **9/9**, sem números não verificados.

**Honestamente:** a rodada completa nunca foi repetida inteira depois das correções (para poupar a cota da API), e as amostras são pequenas; a variação entre execuções existe (ver `--runs`). A planilha é sintética e as perguntas têm resposta única em SQL: perguntas abertas ("quais insights você vê?") não foram avaliadas. Alguns tempos de 20 a 48 s foram retentativas automáticas por limite de cota do plano gratuito, não lentidão do modelo.

`evaluation/` gera uma planilha sintética determinística (600 vendas, 7 preços faltando, uma injeção de prompt escondida numa célula) e faz 10 perguntas cujo gabarito é calculado em pandas. Cada pergunta roda numa sessão nova. O gabarito é conferido contra o DuckDB depois da carga (`tests/test_evaluation_helpers.py`).

No PowerShell, **no seu terminal** (não cole a chave no chat):

```powershell
$env:GOOGLE_API_KEY = "sua-chave"      # ou ponha num .env na raiz do Portfolio
cd C:\Users\Marcio\Documents\python\Portfolio\02-agente-planilhas
..\.venv\Scripts\python -m evaluation.run_eval --only linhas receita_total   # 2 perguntas, custo baixo
..\.venv\Scripts\python -m evaluation.run_eval                                # as 10 perguntas
..\.venv\Scripts\python -m evaluation.run_eval --provider ollama --model llama3.1:8b   # modelo local
..\.venv\Scripts\python -m evaluation.run_eval --model gemini-3.5-flash-lite --runs 3  # outro modelo Google
```

Para comparar com um modelo maior (`--provider anthropic --model claude-sonnet-5-5`), defina também `ANTHROPIC_API_KEY`.

Cada execução grava um JSON em `evaluation/results/` com resposta, SQL usado, tempo e tokens por pergunta. `inexistente` (pergunta sobre lucro, que a planilha não tem) e `injecao` têm checagem aproximada; leia as respostas.

## Observabilidade (Langfuse, opcional)

Sem `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` nada é enviado e nada muda. Com as chaves (as do **projeto Langfuse deste app**), cada pergunta vira um trace `sheet-turn`, agrupado pela sessão:

| Observação | Tipo | O que registra |
|---|---|---|
| `generation` (uma por passo do agente) | generation | prompt completo do passo, resposta, tokens, tempo até o primeiro token e as ferramentas pedidas |
| `run_sql`, `describe_table`, `make_chart` | tool | os argumentos (a consulta SQL), o início do resultado (até 1500 caracteres) e, se falhou, o erro (nível `ERROR`) |

**Notas automáticas** (`app/guardrails.py`; heurísticas, sinais e não bloqueios):

| Nota | O que mede |
|---|---|
| `unverified_numbers` | quantos números grandes da resposta não aparecem em nenhum resultado de consulta (a mesma checagem de ancoragem que avisa o usuário; aqui vira métrica) |
| `tool_errors` | quantas chamadas de ferramenta falharam (mesmo que o modelo tenha se corrigido) |
| `no_query` | a resposta saiu sem nenhuma consulta SQL bem-sucedida (recusa correta ou palpite: leia o trace) |

**Privacidade, leia antes de ligar:** os traces contêm a pergunta, a resposta e o começo dos resultados das consultas, ou seja, **trechos da planilha do visitante**. O nome do arquivo não vai (só o número de tabelas e de linhas); e-mails, CPF e telefones são mascarados, mas o resto dos dados não. Por isso o `.env.example` deixa `LANGFUSE_CAPTURE_CONTENT=false` (só tokens, latência e notas, sem textos). Com `true`, defina a retenção do projeto no Langfuse e avise os usuários. Os testes usam observações falsas e o SDK real com exportador em memória (no `portfolio-core`); **o envio foi confirmado em 2026-11-01 contra um Langfuse 4.72.0 local** com `python -m evaluation.langfuse_smoke` (provedor roteirizado, sem LLM, uma consulta SQL de verdade no DuckDB: chegaram `sheet-turn`, as 2 `generation`, `run_sql` e as 3 notas). Não foi testado com tráfego real nem na VPS.

## Decisões e limites conhecidos

- **DuckDB em memória por sessão**, e não um banco compartilhado: isolamento simples e descarte barato.
- **Loop manual em vez do tool runner do SDK**, para emitir eventos de progresso (SQL, gráfico) durante o streaming e controlar o teto de passos.
- **Histórico entre perguntas guarda só o texto** das respostas, não os resultados das ferramentas; perguntas de acompanhamento que dependem de números anteriores fazem o agente consultar de novo.
- Fórmulas do Excel chegam como o último valor calculado salvo no arquivo; não são recalculadas.
- `.xls` antigo e arquivos protegidos por senha não são suportados.
- O Chart.js vem da CDN (cdnjs, versão fixa); se não carregar, o gráfico mostra um aviso.

## Próximos passos

- [ ] Repetir a rodada completa (10 × 3) com o código atual e comparar com o Ollama local e um modelo maior (modelo × acertos × custo).
- [ ] Avaliar perguntas abertas (insights) e planilhas com colunas ambíguas.
- [ ] Conferir os termos do plano gratuito da API do Google antes de expor a demo: em geral, o plano gratuito pode usar o conteúdo enviado para melhorar produtos, o que não combina com uploads de planilhas de visitantes.
- [ ] Validar os traces contra um servidor Langfuse no ar, calcular o custo por conversa e decidir a retenção.
- [ ] Exportar o relatório também em PDF/DOCX com os gráficos.
- [ ] Cache de prompt do esquema + ferramentas (prefixo estável por sessão).

## Docker e deploy

Este repositório é autossuficiente (repositório GitHub: `portfolio-planilhas`). Arquivos:

| Arquivo | Para quê |
|---|---|
| `Dockerfile` | Imagem em 2 estágios, usuário sem privilégios, `HEALTHCHECK` em `/health`. O `portfolio-core` vem da tag de `requirements-core.txt` |
| `compose.yml` | Produção: puxa `ghcr.io/hmarcio2/portfolio-planilhas`, entra na rede `edge`, `read_only`, sem capabilities, limite de memória |
| `compose.local.yml` | Desenvolvimento: constrói a imagem com o `../_core` local e publica a porta 8002 |
| `deploy/planilhas.caddy` | Trecho de Caddy instalado em `/opt/edge/sites/planilhas.caddy` pelo deploy: o subdomínio `planilhas.SEUDOMINIO` |
| `.github/workflows/ci.yml` | Em todo push/PR: testes (Python 3.12), build da imagem e `/health` no container |
| `.github/workflows/deploy.yml` | Build → GHCR → SSH → `docker compose up --wait` → recarrega o Caddy. **Manual por enquanto** (`workflow_dispatch`); para automatizar, troque o gatilho por push na `main` |
| `.env.example` | Variáveis do app. Na VPS, copie para `/opt/apps/planilhas/.env` |

Rodar com Docker na sua máquina:

```bash
cp .env.example .env                      # preencha GOOGLE_API_KEY
docker compose -f compose.yml -f compose.local.yml up --build
```
Abra `http://localhost:8002`.

Os uploads vivem em `/tmp` do container, que é `tmpfs` (512 MB): nada persiste em disco e tudo some ao reiniciar.

**Deploy na VPS:** configure uma vez a VPS e os segredos do ambiente `production` conforme o README do `portfolio-edge`
(`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS`), crie `/opt/apps/planilhas/.env` e rode `Actions > Deploy`.
Para voltar a uma versão: na VPS, `cd /opt/apps/planilhas && IMAGE_TAG=sha-<commit-antigo> docker compose up -d --wait`.
