"""
streamlit_app.py — Track Position Seller, painel do lojista.

Este repositório (`sellers_app`) existe só para o deploy no Streamlit
Community Cloud — o código-fonte e o histórico de decisão vivem em
`ederrabelo81-crypto/RAC-Position-tracker`, pasta `seller_app/`. Alterações
de fundo (lógica, correções, testes) entram por lá; este arquivo é publicado
aqui via sincronização, não editado direto.

APLICAÇÃO SEPARADA do `app.py` do RAC Position Tracker, de propósito:

  * aquele é interno, visão da indústria, e segura uma chave que lê tudo;
  * este aqui atende gente de fora e **não tem chave de escrita**: lê só
    `seller_offer_daily`, `seller_coverage_daily` e a view de share, com a
    chave `anon` e RLS ligada (migração 016 do RAC-Position-tracker).

Pôr uma página de tenant dentro do app interno transformaria o isolamento num
`WHERE` em Python: um bug de filtro e o Dufrio vê o plano do Frigelar. Aqui a
fronteira é a chave e a policy, não o código.

Rodar local:
    streamlit run streamlit_app.py

Segredos (`.streamlit/secrets.toml` local, ou Settings → Secrets no painel do
Streamlit Cloud):
    SUPABASE_URL = "https://<projeto>.supabase.co"
    SUPABASE_ANON_KEY = "<chave anon — NUNCA a service_role>"
    SELLER = "Web Continental"       # trava o painel num seller só
    SENHA = "<senha da demo>"        # opcional; sem ela o painel fica aberto
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st
from supabase import create_client

st.set_page_config(page_title="Track Position Seller", page_icon="📦", layout="wide")

# ── Cores: verde-petróleo do produto, âmbar para "não observado" ────────────
ACENTO, ALERTA, NEUTRO = "#1B6E6A", "#9C5D11", "#56635F"


@st.cache_resource
def _client():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])


PAGINA = 1000  # teto por chamada do PostgREST; ler além disso exige paginar


def _todas_as_linhas(consulta, ordenar_por: list[str]) -> list[dict]:
    """Lê a consulta inteira, página a página.

    O PostgREST devolve no máximo 1.000 linhas e **não avisa** que truncou:
    um seller com 2.865 linhas na janela apareceria com um terço do histórico,
    subestimando ofertas, viradas e cobertura sem nenhum sinal na tela. A
    ordenação estável é o que garante que as páginas não se sobreponham nem
    pulem linhas entre chamadas.
    """
    for coluna in ordenar_por:
        consulta = consulta.order(coluna)
    linhas: list[dict] = []
    inicio = 0
    while True:
        lote = consulta.range(inicio, inicio + PAGINA - 1).execute().data or []
        linhas.extend(lote)
        if len(lote) < PAGINA:
            return linhas
        inicio += PAGINA


@st.cache_data(ttl=900)
def carregar(seller: str, desde: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fato do seller, share e cobertura. TTL de 15 min — a coleta é 3x/dia."""
    cli = _client()
    d = desde.isoformat()

    fato = pd.DataFrame(_todas_as_linhas(
        cli.table("seller_offer_daily").select(
            "data,turno,plataforma,superficie,offer_key,produto,marca,preco,posicao_melhor,"
            "keywords_presente,detentor_buybox,detentor_anterior,virou_no_turno,"
            "qtd_sellers,identidade_suspeita"
        ).eq("seller_canonical", seller).gte("data", d),
        ["data", "turno", "plataforma", "offer_key"]))

    share = pd.DataFrame(_todas_as_linhas(
        cli.table("v_seller_buybox_share").select("*")
        .eq("seller_canonical", seller).gte("data", d),
        ["data", "plataforma"]))

    cobertura = pd.DataFrame(_todas_as_linhas(
        cli.table("seller_coverage_daily").select("*").gte("data", d),
        ["data", "turno", "plataforma"]))

    return fato, share, cobertura


def _porta() -> bool:
    """Senha simples da demo. Não é autenticação de tenant — é um cadeado."""
    senha = st.secrets.get("SENHA")
    if not senha:
        return True
    if st.session_state.get("liberado"):
        return True
    st.title("Track Position Seller")
    digitada = st.text_input("Senha", type="password")
    if digitada and digitada == senha:
        st.session_state["liberado"] = True
        st.rerun()
    elif digitada:
        st.error("Senha incorreta.")
    return False


def main() -> None:
    if not _porta():
        return

    seller = st.secrets.get("SELLER", "Web Continental")
    dias = st.sidebar.slider("Janela (dias)", 3, 60, 14)
    desde = date.today() - timedelta(days=dias)

    st.title(f"Track Position Seller — {seller}")
    st.caption(
        "Buy box, posição e concorrência nos marketplaces. Coleta em 3 turnos "
        "(08h Abertura · 14h Tarde · 20h Fechamento)."
    )

    fato, share, cobertura = carregar(seller, desde)

    vazio = fato.empty
    if vazio:
        # Sem KPI, mas as abas CONTINUAM: a de Cobertura é justamente o que
        # separa "não houve oferta" de "a coleta não rodou", e esconder ela
        # aqui tiraria a resposta exatamente no caso em que a pergunta importa.
        st.warning(
            f"Sem oferta registrada para **{seller}** desde {desde:%d/%m}. "
            "Isso pode ser ausência real **ou** coleta que não rodou — a aba "
            "**Cobertura** abaixo diz qual dos dois."
        )

    # ── Só marketplace entra em KPI. Loja própria o lojista joga sozinho, e
    #    identidade suspeita é chave colapsada: nem numerador, nem denominador.
    limpo = (fato[(fato["superficie"] == "marketplace") & (~fato["identidade_suspeita"])]
             if not vazio else fato)

    if not vazio:
        c1, c2, c3, c4 = st.columns(4)
        if not share.empty:
            ultimo_dia = share["data"].max()
            hoje = share[share["data"] == ultimo_dia]
            detidos, universo = hoje["produtos_detidos"].sum(), hoje["produtos_universo"].sum()
            # Share real = soma/soma. A média dos percentuais por plataforma
            # daria outro número quando os universos diferem (Magalu 661 x
            # ML 307), e não seria o share de nada.
            pct = 100.0 * detidos / universo if universo else 0.0
            c1.metric("Share de buy box", f"{pct:.1f}%",
                      help=f"{int(detidos)} de {int(universo)} produtos com buy box "
                           f"observada em {ultimo_dia}, somando as plataformas.")
            c2.metric("Produtos com a caixa", int(detidos),
                      help=f"de {int(universo)} observados")
        c3.metric("Ofertas monitoradas",
                  f"{limpo['offer_key'].nunique():,}".replace(",", "."))
        viradas = int(limpo["virou_no_turno"].fillna(False).sum())
        c4.metric("Viradas de buy box", viradas,
                  help="Produtos em que a caixa trocou de dono entre turnos.")

    aba1, aba2, aba3 = st.tabs(["📈 Share de buy box", "🔄 Viradas", "🩺 Cobertura"])

    with aba1:
        if share.empty:
            st.info("Sem share no período — nenhum produto seu detinha a caixa.")
        else:
            pivo = share.pivot_table(index="data", columns="plataforma",
                                     values="share_buybox_pct", aggfunc="mean")
            st.line_chart(pivo, height=320)
            st.dataframe(
                share.sort_values(["data", "share_buybox_pct"], ascending=[False, False])[
                    ["data", "plataforma", "produtos_detidos", "produtos_universo",
                     "share_buybox_pct"]
                ].rename(columns={
                    "data": "Data", "plataforma": "Plataforma",
                    "produtos_detidos": "Com a caixa",
                    "produtos_universo": "Universo observado",
                    "share_buybox_pct": "Share %"}),
                use_container_width=True, hide_index=True)
            st.caption(
                "O denominador é o universo de produtos **observados** na "
                "plataforma, não as suas linhas. Sobre as suas linhas o número "
                "daria sempre 100%: a vitrine só mostra quem detém a caixa."
            )

    with aba2:
        v = limpo[limpo["virou_no_turno"].fillna(False)]
        if v.empty:
            st.info("Nenhuma virada de buy box observada na janela.")
        else:
            st.dataframe(
                v[["data", "turno", "plataforma", "produto", "detentor_anterior",
                   "preco"]].sort_values("data", ascending=False)
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "detentor_anterior": "Detinha antes",
                    "preco": "Preço (R$)"}),
                use_container_width=True, hide_index=True)

    with aba3:
        st.markdown(
            "**Coleta ausente não é mercado vazio.** Turno sem leitura aparece "
            "aqui como não observado, e as células dele ficam fora de todo "
            "número acima — nunca viram zero."
        )
        if cobertura.empty:
            st.warning("Sem registro de cobertura no período.")
        else:
            # Conta turno OBSERVADO. Contar linhas presentes daria cobertura
            # completa mesmo quando as três células estão marcadas como não
            # observadas — que é exatamente o caso que esta aba existe para expor.
            resumo = (cobertura.assign(observado=cobertura["observado"].astype(bool))
                      .groupby(["data", "plataforma"])
                      .agg(turnos_observados=("observado", "sum"),
                           linhas=("linhas", "sum"))
                      .reset_index())
            faltantes = resumo[resumo["turnos_observados"] < 3]
            if faltantes.empty:
                st.success("Os 3 turnos foram observados em todas as plataformas.")
            else:
                st.warning(f"{len(faltantes)} combinações data×plataforma com menos de 3 turnos.")
                st.dataframe(faltantes, use_container_width=True, hide_index=True)

    suspeitas = int(fato["identidade_suspeita"].sum())
    if suspeitas:
        st.divider()
        st.caption(
            f"⚠️ {suspeitas} ofertas ficaram fora dos números por identidade "
            "ambígua (chave de oferta colapsada na origem). Estão excluídas de "
            "propósito: entrariam somando produtos diferentes como se fossem um."
        )


if __name__ == "__main__":
    main()
