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

```toml
SUPABASE_URL = "https://<projeto>.supabase.co"
SUPABASE_ANON_KEY = "<chave anon — NUNCA a service_role>"
SELLER = "Web Continental"       # trava o painel num seller só
SENHA = "<senha da demo>"        # opcional; sem ela o painel fica aberto
```

**A chave é sempre a `anon`.** O segredo do Streamlit Cloud é visível a quem
administra o app, e a `service_role` ignora RLS — vazá-la dá escrita no banco
inteiro.

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
