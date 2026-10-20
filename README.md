# Track Position Seller — painel do lojista

Painel de buy box, posição e cobertura para sellers do mercado de ar
condicionado, servido a partir do [RAC Position
Tracker](https://github.com/ederrabelo81-crypto/RAC-Position-tracker).

**Este repositório existe só para o deploy no Streamlit Community Cloud.** O
código-fonte, a documentação de arquitetura e o histórico de decisão vivem em
`RAC-Position-tracker`, pasta [`seller_app/`](https://github.com/ederrabelo81-crypto/RAC-Position-tracker/tree/main/seller_app) —
qualquer mudança de lógica entra por lá e é sincronizada para cá, não editada
direto aqui.

## Por que um repositório separado

O `app.py` do RAC Position Tracker é interno: visão da indústria, chave que lê
tudo. Este painel atende gente de fora — um seller — e **não carrega chave de
escrita**: lê só `seller_offer_daily`, `seller_coverage_daily` e a view de
share, com a chave `anon` e RLS ligada.

## Segredos (Settings → Secrets, no painel do Streamlit Cloud)

Dois bancos possíveis — a fronteira é qual secret existe, não um `if` no
código (mesma regra do resto do projeto):

```toml
# Preferencial (Set/2026): Postgres na Aiven — ver "Banco de dados" abaixo.
RAC_DB_DSN = "postgresql://seller_ro:<senha>@<host>.aivencloud.com:<porta>/defaultdb?sslmode=require"

# Legado: só é usado se RAC_DB_DSN estiver ausente (ex.: a cota do Supabase
# voltou a caber no free tier).
SUPABASE_URL = "https://<projeto>.supabase.co"
SUPABASE_ANON_KEY = "<chave anon — NUNCA a service_role>"

SELLER = "Web Continental"       # trava o painel num seller só
SENHA = "<senha da demo>"        # opcional; sem ela o painel fica aberto
```

**A credencial é sempre só-leitura.** No Supabase é a chave `anon` com RLS
ligada; na Aiven é o usuário `seller_ro`, com `GRANT SELECT` só nas 3
tabelas/views que o painel lê (nunca o usuário `avnadmin`, que tem DDL/DML
completo).

## Banco de dados — migração Supabase → Aiven (Set/2026)

Em 12/09/2026 o projeto Supabase estourou a cota do free tier (500 MB) e o
PostgREST — a API REST que o `supabase-py` consome — passou a devolver HTTP
402 (`exceed_db_size_quota`) em toda leitura, mesmo com o Postgres saudável
por trás (é esse o erro `postgrest.exceptions.APIError` que este painel
mostrava). O [RAC Position
Tracker](https://github.com/ederrabelo81-crypto/RAC-Position-tracker) migrou
a janela quente para um Postgres na Aiven — passo a passo completo em
[`docs/MIGRACAO_AIVEN.md`](https://github.com/ederrabelo81-crypto/RAC-Position-tracker/blob/main/docs/MIGRACAO_AIVEN.md)
daquele repositório.

Este painel foi deixado de fora daquela migração de propósito (troca de
banco é troca de credencial, não deveria pegar carona em outra mudança) — mas
já fala com os dois bancos: com `RAC_DB_DSN` presente nos secrets, ele usa um
adaptador mínimo sobre `psycopg2` (a mesma ideia do `utils/db.py` do
RAC-Position-tracker, recortada para `select/eq/gte/order/range`, só
leitura); sem ele, segue no Supabase como antes.

**Para ativar a Aiven**, crie o usuário só-leitura no banco novo (mesmo SQL
já documentado no `MIGRACAO_AIVEN.md`):

```sql
CREATE ROLE seller_ro LOGIN PASSWORD 'troque-isto';
GRANT CONNECT ON DATABASE defaultdb TO seller_ro;
GRANT USAGE ON SCHEMA public TO seller_ro;
GRANT SELECT ON seller_offer_daily, seller_coverage_daily, v_seller_buybox_share TO seller_ro;
```

e publique `RAC_DB_DSN = "postgresql://seller_ro:<senha>@<host>.aivencloud.com:<porta>/defaultdb?sslmode=require"`
em Settings → Secrets no painel do Streamlit Cloud (mantenha o
`?sslmode=require` — a Aiven recusa conexão sem TLS).

## Rodar local

```bash
uv sync
mkdir -p .streamlit && cat > .streamlit/secrets.toml <<'TOML'
SUPABASE_URL = "https://<projeto>.supabase.co"
SUPABASE_ANON_KEY = "<chave anon>"
SELLER = "Web Continental"
TOML
uv run streamlit run streamlit_app.py
```

## Resumo diário para WhatsApp (`scripts/resumo_diario.py`)

Exceção à regra "só deploy": este script ainda não tem casa no
RAC-Position-tracker e nasce aqui para os pilotos pagos. Ele não toca no
painel e o Streamlit Cloud o ignora. Lê as mesmas três fontes, com a mesma
credencial só-leitura (`RAC_DB_DSN`, do ambiente ou de
`.streamlit/secrets.toml`), e devolve um texto com a marcação do WhatsApp:
share de buy box com variação por plataforma, ganhos × perdas nas últimas 24h,
o rival que mais tirou buy box do seller e até três ações (perda a retomar com
preço-alvo, queda na busca, produto em que o seller está sozinho).

```bash
python scripts/resumo_diario.py --demo                       # dados sintéticos, sem banco
python scripts/resumo_diario.py --seller "Web Continental"   # imprime na tela
python scripts/resumo_diario.py --seller "A" --seller "B" --saida resumos/   # um .txt por seller
```

Rodado logo depois do turno das 08h, o resumo cobre a noite e o dia
anterior. Turno não coletado aparece como aviso no rodapé e não entra como
zero. O preço-alvo sai do preço do rival que levou a buy box. Se o seller já
estava mais barato, o texto aponta frete, prazo e reputação em vez de mandar
baixar o preço.

### Dentro do painel

A aba **📲 Resumo para WhatsApp** do `streamlit_app.py` gera o mesmo texto sem
precisar rodar nada local: escolha um ou mais sellers (só na instância sem
`SELLER` travado), o dia de referência, o máximo de ações e os tipos de ação,
e copie cada quadro pelo ícone no canto. O filtro de plataforma da sidebar vale
para ela também. A aba importa `scripts/resumo_diario.py` em vez de copiar a
lógica, então o texto do painel e o do script são sempre iguais.

**Atenção na sincronização abaixo:** o `seller_app/app.py` do
RAC-Position-tracker ainda não tem esta aba. Copiar aquele arquivo por cima do
`streamlit_app.py` apaga a aba. Leve a mudança para lá antes da próxima
sincronização, junto com `scripts/resumo_diario.py`.

## Como sincronizar depois de uma mudança no RAC-Position-tracker

```bash
cp ../RAC-Position-tracker/seller_app/app.py streamlit_app.py
# revisar o docstring do topo do arquivo se o caminho tiver mudado
git add streamlit_app.py && git commit -m "sync: seller_app/app.py do RAC-Position-tracker" && git push
```

O push para `main` dispara redeploy automático no Streamlit Cloud — nenhuma
configuração do painel precisa mudar, porque o arquivo de entrada continua
sendo `streamlit_app.py`.

## Onde o painel se recusa a responder — por desenho

- **Loja própria não entra em KPI.** Lá o lojista joga sozinho e detém 100% da
  própria vitrine; somar isso ao share inflaria o número.
- **Oferta com identidade ambígua fica de fora.** Quando a chave de oferta
  colapsa na origem (hoje: parte do Google Shopping e do Mercado Livre), ela
  some dos números — visível no rodapé do painel, nunca somada em silêncio.
- **Turno não coletado não vira zero.** A aba Cobertura existe para separar
  "não houve oferta" de "não olhamos".

## Pré-requisito no banco

O painel lê `seller_offer_daily`, `seller_coverage_daily` e
`v_seller_buybox_share` — criadas pelas migrações
[`016`](https://github.com/ederrabelo81-crypto/RAC-Position-tracker/blob/main/docs/migrations/016_seller_offer_daily.sql)
e
[`017`](https://github.com/ederrabelo81-crypto/RAC-Position-tracker/blob/main/docs/migrations/017_seller_offer_daily_correcoes.sql)
do RAC-Position-tracker, já aplicadas em produção. Sem elas o painel sobe mas
não mostra nada.
