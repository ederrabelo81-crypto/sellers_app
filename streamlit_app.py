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

Nada aqui é dado privado — tudo é observado-público (§2.3 do documento): a
vitrine mostra o mesmo preço e o mesmo vencedor de buy box para qualquer
visitante do marketplace. É por isso que o painel pode nomear e comparar
concorrentes livremente (aba Ranking) sem violar isolamento nenhum; o dia em
que houver custo, margem ou estoque do próprio tenant, esse dado nasce em
outro lugar (o TPS da Fase 2) e nunca entra nesta base.

Rodar local:
    streamlit run streamlit_app.py

Segredos (`.streamlit/secrets.toml` local, ou Settings → Secrets no painel do
Streamlit Cloud):
    SUPABASE_URL = "https://<projeto>.supabase.co"
    SUPABASE_ANON_KEY = "<chave anon — NUNCA a service_role>"
    SELLER = "Web Continental"       # opcional: trava o painel num seller só.
                                      # Ausente = seletor livre entre todos os
                                      # sellers com dado na janela (uso: demo
                                      # compartilhada). Presente = pensado para
                                      # uma instância dedicada por tenant.
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

# `numeric`/`decimal` do Postgres chega pelo PostgREST como STRING JSON, não
# número — é assim que a API evita perder precisão de ponto flutuante em
# coluna de dinheiro. Sem esta conversão, `share.pivot_table(..., aggfunc=
# "mean")` quebra com "dtype 'str' does not support operation 'mean'" — bug
# real que chegou a ir para produção porque nenhum teste anterior tocava
# dado de verdade (o sandbox de desenvolvimento não alcança o Supabase).
_COLUNAS_NUMERICAS = {"share_buybox_pct", "preco", "posicao_mediana"}


def _tipar(df: pd.DataFrame) -> pd.DataFrame:
    """Converte para número as colunas `numeric` do Postgres, uma vez na
    borda de entrada — para que nenhum código adiante precise lembrar disso."""
    for col in _COLUNAS_NUMERICAS & set(df.columns):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# As colunas pedidas ao PostgREST, uma vez só: o mesmo texto vai no `select()`
# e no esqueleto do dataframe vazio, para os dois não divergirem em silêncio.
_SELECT_FATO = (
    "data,turno,plataforma,superficie,offer_key,marketplace_product_id,produto,marca,"
    "preco,posicao_melhor,posicao_mediana,keywords_presente,detentor_buybox,"
    "detentor_anterior,virou_no_turno,qtd_sellers,tipo_seller,identidade_suspeita"
)
_SELECT_PERDIDOS = "data,turno,plataforma,seller_canonical,produto,marca,preco"


def _quadro(linhas: list[dict], select: str) -> pd.DataFrame:
    """DataFrame que carrega o ESQUEMA mesmo quando não veio nenhuma linha.

    `pd.DataFrame([])` não tem zero linhas: tem zero linhas **e zero
    colunas**. Todo `df["coluna"]` adiante vira `KeyError`, e no Streamlit
    isso não é uma tabela vazia na tela — é a página inteira morrendo antes
    de renderizar. Foi o que aconteceu quando o `SELLER` do secrets passou a
    nomear uma grafia que a canonização aposentou: a consulta voltava vazia,
    o aviso "sem oferta registrada" era escrito, e a linha seguinte derrubava
    o app com `KeyError: 'virou_no_turno'` — as abas que o aviso prometia
    ("a de Cobertura diz qual dos dois") nunca chegavam a existir.

    Declarar as colunas resolve na borda de entrada, como o `_tipar`: com a
    lista vazia o pandas materializa o esqueleto, e com linhas ele fixa
    ordem e conjunto — a resposta do PostgREST nunca contradiz o `select`.
    """
    return pd.DataFrame(linhas, columns=select.split(","))


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
def carregar_mercado(desde: date) -> pd.DataFrame:
    """Share de TODOS os sellers na janela, sem filtro por seller.

    Uma única consulta alimenta duas coisas: a lista do seletor (§ picker) e o
    ranking por plataforma (aba Ranking). É dado público — a mesma vitrine que
    qualquer visitante do marketplace vê — então nomear e ordenar concorrentes
    aqui não fura fronteira nenhuma de tenant.
    """
    cli = _client()
    linhas = _todas_as_linhas(
        cli.table("v_seller_buybox_share").select("*").gte("data", desde.isoformat()),
        # `seller_canonical` como 3º critério não é enfeite: é o que torna a
        # ordenação ÚNICA. `(data, plataforma)` se repete em toda linha do
        # mesmo dia/plataforma — sem desempate, paginação por OFFSET/LIMIT
        # não garante ordem estável entre chamadas sucessivas, e linha pode
        # sumir ou duplicar na fronteira de página (aqui, 1204 > PAGINA=1000,
        # cruza página de verdade). `(data, plataforma, seller_canonical)` é
        # a chave de agrupamento da própria view — de fato única.
        ["data", "plataforma", "seller_canonical"])
    return _tipar(pd.DataFrame(linhas))


@st.cache_data(ttl=900)
def carregar(seller: str, desde: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fato do seller, share e cobertura. TTL de 15 min — a coleta é 3x/dia."""
    cli = _client()
    d = desde.isoformat()

    fato = _tipar(_quadro(_todas_as_linhas(
        cli.table("seller_offer_daily").select(_SELECT_FATO)
        .eq("seller_canonical", seller).gte("data", d),
        ["data", "turno", "plataforma", "offer_key"]), _SELECT_FATO))

    share = _tipar(pd.DataFrame(_todas_as_linhas(
        cli.table("v_seller_buybox_share").select("*")
        .eq("seller_canonical", seller).gte("data", d),
        ["data", "plataforma"])))

    cobertura = pd.DataFrame(_todas_as_linhas(
        cli.table("seller_coverage_daily").select("*").gte("data", d),
        ["data", "turno", "plataforma"]))

    return fato, share, cobertura


@st.cache_data(ttl=900)
def carregar_perdidos(seller: str, desde: date) -> pd.DataFrame:
    """Produtos em que ESTE seller detinha a caixa e outro tomou.

    `seller_offer_daily` só tem linha do DETENTOR (a SERP não mostra
    perdedor) — por isso "ganhei" já sai de graça filtrando pelas próprias
    linhas (`detentor_anterior` preenchido nelas). "Perdi" é o espelho: a
    linha pertence a OUTRO seller, e é ele quem carrega `detentor_anterior`
    apontando para este. Consulta separada porque o filtro é por uma coluna
    diferente da que trava o resto da página.
    """
    cli = _client()
    linhas = _todas_as_linhas(
        cli.table("seller_offer_daily").select(_SELECT_PERDIDOS)
        .eq("detentor_anterior", seller).eq("virou_no_turno", True)
          .eq("identidade_suspeita", False).gte("data", desde.isoformat()),
        ["data", "turno", "plataforma"])
    return _tipar(_quadro(linhas, _SELECT_PERDIDOS))


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


def _escolher_seller(desde: date) -> tuple[str | None, pd.DataFrame]:
    """Decide o seller da sessão: travado por secret, ou escolhido na sidebar.

    Retorna também o mercado (todos os sellers) já carregado, para não buscar
    duas vezes — o ranking da aba própria reusa o mesmo dataframe.
    """
    mercado = carregar_mercado(desde)
    travado = st.secrets.get("SELLER")

    if travado:
        st.sidebar.caption(f"Instância travada em **{travado}**.")
        return travado, mercado

    if mercado.empty:
        st.sidebar.error("Sem sellers com dado na janela selecionada.")
        return None, mercado

    ranking = (mercado.groupby("seller_canonical")["produtos_detidos"]
               .sum().sort_values(ascending=False))
    lista = ranking.index.tolist()
    padrao = lista.index("Web Continental") if "Web Continental" in lista else 0
    escolhido = st.sidebar.selectbox(
        "Seller", lista, index=padrao,
        help="Todo dado aqui é observado-público — a mesma vitrine que "
             "qualquer visitante do marketplace vê.")
    return escolhido, mercado


def main() -> None:
    if not _porta():
        return

    dias = st.sidebar.slider("Janela (dias)", 3, 60, 14)
    desde = date.today() - timedelta(days=dias)

    seller, mercado = _escolher_seller(desde)
    if not seller:
        return

    st.title(f"Track Position Seller — {seller}")
    st.caption(
        "Buy box, posição e concorrência nos marketplaces. Coleta em 3 turnos "
        "(08h Abertura · 14h Tarde · 20h Fechamento)."
    )

    fato, share, cobertura = carregar(seller, desde)
    perdidos = carregar_perdidos(seller, desde)

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
        if st.secrets.get("SELLER"):
            # TERCEIRA causa possível, que a aba Cobertura NÃO consegue
            # descartar: instância travada num nome que a canonização
            # aposentou. O de-para (`utils.seller_names`) colapsa grafias num
            # canônico — quando "Comprebel" passa a ser variante de
            # "Bel Micro", ou "GoCompras" de "Denteck", o secret que ainda
            # nomeia a grafia velha aponta para um seller que deixou de
            # existir e o PostgREST devolve `[]` com HTTP 200.
            #
            # É HIPÓTESE, não diagnóstico, e o texto tem de dizer isso: seller
            # novo, dia parado e coleta que não rodou produzem exatamente este
            # mesmo estado. Afirmar "o nome está errado" mandaria o operador
            # mexer no secret certo. O que não dá é ficar calado — esta é a
            # única das três causas com conserto fora do banco, e ela não
            # aparece em lugar nenhum da tela.
            #
            # Não dá para estreitar pelo `mercado`: a view sai do próprio
            # `seller_offer_daily`, então seller ausente do fato está ausente
            # da view por construção — a checagem seria sempre verdadeira e
            # não separaria nada. Quem separa de fato é a aba Cobertura, para
            # as outras duas causas.
            st.info(
                f"**Se a coleta rodou** (veja a aba Cobertura), sobra conferir o "
                f"nome. Esta instância está travada em **{seller}** pelo secret "
                "`SELLER`, comparado **literalmente** com `seller_canonical` — e "
                "a canonização aposenta grafias: quando uma vira variante de "
                "outra, o canônico muda e o secret velho passa a apontar para um "
                "seller que não existe mais, sem erro nenhum na resposta."
            )
            if not mercado.empty:
                nomes = (mercado.groupby("seller_canonical")["produtos_detidos"]
                         .sum().sort_values(ascending=False).index.tolist())
                st.caption(
                    "Sellers com buy box observada na janela: "
                    + ", ".join(f"`{n}`" for n in nomes[:15])
                    + (f" … e mais {len(nomes) - 15}." if len(nomes) > 15 else "."))

    # ── Só marketplace entra em KPI. Loja própria o lojista joga sozinho, e
    #    identidade suspeita é chave colapsada: nem numerador, nem denominador.
    limpo = (fato[(fato["superficie"] == "marketplace") & (~fato["identidade_suspeita"])]
             if not vazio else fato)

    if not vazio:
        c1, c2, c3, c4, c5 = st.columns(5)
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
        ganhos_n = int(limpo["virou_no_turno"].fillna(False).sum())
        perdidos_n = len(perdidos)
        c4.metric("Ganhos de buy box", ganhos_n,
                  help="Produtos em que este seller tomou a caixa de outro.")
        c5.metric("Perdidos", perdidos_n,
                  help="Produtos em que outro seller tomou a caixa deste.",
                  delta=f"{ganhos_n - perdidos_n:+d} no saldo" if (ganhos_n or perdidos_n) else None)
                  # delta_color fica no padrão "normal" de propósito: saldo
                  # negativo já pinta vermelho sozinho. "inverse" faria o
                  # OPOSTO do pretendido — pintaria o pior saldo de verde,
                  # que é a cor certa só para métrica onde diminuir é bom
                  # (latência, custo) — não é o caso de perder buy box.

    aba1, aba2, aba3, aba4, aba5 = st.tabs([
        "📈 Share de buy box", "🏆 Ganhos e perdas", "🏷️ Marcas e posição",
        "🥊 Ranking na plataforma", "🩺 Cobertura",
    ])

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
        col_g, col_p = st.columns(2)
        col_g.metric("Ganhos na janela", len(limpo[limpo["virou_no_turno"].fillna(False)]))
        col_p.metric("Perdidos na janela", len(perdidos))
        st.caption(
            "Ganhei = tomei a caixa de outro seller. Perdi = outro seller tomou "
            "a minha. Os dois só existem como EVENTO — a foto de um turno nunca "
            "mostra \"perdedor\", só quem está com a caixa agora."
        )

        st.markdown("##### ✅ Produtos que você ganhou")
        ganhos = limpo[limpo["virou_no_turno"].fillna(False)]
        if ganhos.empty:
            st.info("Nenhum ganho de buy box observado na janela.")
        else:
            st.dataframe(
                ganhos[["data", "turno", "plataforma", "produto", "marca",
                        "detentor_anterior", "preco"]]
                .sort_values("data", ascending=False)
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "marca": "Marca",
                    "detentor_anterior": "Tomou de", "preco": "Preço (R$)"}),
                use_container_width=True, hide_index=True)

        st.markdown("##### ❌ Produtos que você perdeu")
        if perdidos.empty:
            st.info("Nenhuma perda de buy box observada na janela.")
        else:
            st.dataframe(
                perdidos[["data", "turno", "plataforma", "produto", "marca",
                          "seller_canonical", "preco"]]
                .sort_values("data", ascending=False)
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "marca": "Marca",
                    "seller_canonical": "Levou", "preco": "Preço (R$)"}),
                use_container_width=True, hide_index=True)

    with aba3:
        detidos_marca = limpo[limpo["detentor_buybox"] == True]  # noqa: E712
        st.markdown("##### Portfólio — produtos detidos por marca")
        if detidos_marca.empty:
            st.info("Sem produto com buy box detida na janela.")
        else:
            # Conta PRODUTO (marketplace_product_id), não oferta: a mesma
            # ligação produto+marca pode render mais de uma offer_key ao
            # longo da janela (a URL canônica muda, ou o produto cai no
            # degrau de hash em vez do de id) — contar offer_key infla a
            # marca. Fallback pro offer_key só nas linhas sem id de produto.
            chave_produto = detidos_marca["marketplace_product_id"].fillna(
                detidos_marca["offer_key"])
            por_marca = (detidos_marca.assign(_produto=chave_produto)
                         .groupby("marca")["_produto"]
                         .nunique().sort_values(ascending=False))
            st.bar_chart(por_marca, height=280)
            sem_buybox_exposta = int(limpo["detentor_buybox"].isna().sum())
            if sem_buybox_exposta:
                st.caption(
                    f"⚠️ {sem_buybox_exposta} ofertas ficaram fora deste gráfico: "
                    "estão em plataformas que não expõem vencedor de buy box na "
                    "vitrine (Amazon, Casas Bahia)."
                )

        st.markdown("##### Posição mediana por plataforma")
        if limpo.empty or limpo["posicao_mediana"].isna().all():
            st.info("Sem dado de posição na janela.")
        else:
            # median(), não mean(): a coluna já é a mediana POR OFERTA
            # (entre keywords, dentro de um turno); agregar várias ofertas
            # com mean() vira "média das medianas", que não é o que o
            # título promete. median() é o mais próximo que dá pra honrar
            # o rótulo sem ter a posição bruta por keyword nesta camada.
            pos = (limpo.groupby(["data", "plataforma"])["posicao_mediana"]
                  .median().reset_index()
                  .pivot(index="data", columns="plataforma", values="posicao_mediana"))
            st.line_chart(pos, height=280)
            st.caption(
                "Quanto menor, melhor — é a posição mediana entre as keywords em "
                "que a oferta apareceu no turno. Não soma entre plataformas nem "
                "vira ranking absoluto: é relativa a cada busca."
            )

        if not limpo.empty and limpo["tipo_seller"].notna().any():
            st.markdown("##### Como você aparece na vitrine")
            tipos = limpo["tipo_seller"].value_counts()
            st.dataframe(
                tipos.rename_axis("Tipo de seller").reset_index(name="Ofertas"),
                use_container_width=True, hide_index=True)

    with aba4:
        plataformas_do_seller = sorted(share["plataforma"].unique()) if not share.empty else []
        if not plataformas_do_seller or mercado.empty:
            st.info("Sem ranking disponível — este seller não detém buy box em nenhuma plataforma na janela.")
        else:
            ultimo_dia_mercado = mercado["data"].max()
            st.caption(f"Ranking observado em {ultimo_dia_mercado} — fotografia do último dia da janela.")
            foto = mercado[mercado["data"] == ultimo_dia_mercado].copy()
            foto["posicao"] = foto.groupby("plataforma")["share_buybox_pct"] \
                                   .rank(ascending=False, method="min").astype(int)
            for plataforma in plataformas_do_seller:
                bloco = foto[foto["plataforma"] == plataforma].sort_values("posicao")
                total = len(bloco)
                linha_seller = bloco[bloco["seller_canonical"] == seller]
                if linha_seller.empty:
                    continue
                minha_posicao = int(linha_seller["posicao"].iloc[0])
                st.markdown(f"##### {plataforma} — você é **#{minha_posicao} de {total}**")
                topo = bloco.head(8).copy()
                if minha_posicao > 8:
                    topo = pd.concat([topo, linha_seller])
                topo["Você"] = topo["seller_canonical"].eq(seller).map({True: "✅", False: ""})
                st.dataframe(
                    topo[["posicao", "seller_canonical", "Você", "produtos_detidos",
                          "produtos_universo", "share_buybox_pct"]]
                    .rename(columns={
                        "posicao": "#", "seller_canonical": "Seller",
                        "produtos_detidos": "Com a caixa",
                        "produtos_universo": "Universo", "share_buybox_pct": "Share %"}),
                    use_container_width=True, hide_index=True)

    with aba5:
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

    suspeitas = int(fato["identidade_suspeita"].sum()) if not vazio else 0
    if suspeitas:
        st.divider()
        st.caption(
            f"⚠️ {suspeitas} ofertas ficaram fora dos números por identidade "
            "ambígua (chave de oferta colapsada na origem). Estão excluídas de "
            "propósito: entrariam somando produtos diferentes como se fossem um."
        )


if __name__ == "__main__":
    main()
