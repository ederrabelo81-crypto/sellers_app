"""
resumo_diario.py — resumo do dia do seller, pronto para colar no WhatsApp.

É a "camada 1" do produto pago: em vez de o seller abrir o painel, ele recebe
depois do turno da manhã três números (buy box, ganhos × perdas, rival que
mais tirou dele) e até três ações concretas. Lê as MESMAS fontes do painel e
com a MESMA credencial só-leitura (`seller_ro`):

    seller_offer_daily      fato por oferta/turno (só linhas do DETENTOR)
    v_seller_buybox_share   share por dia × plataforma (lida dia a dia)
    seller_coverage_daily   quais turnos foram de fato coletados

Uso:

    # credencial em RAC_DB_DSN (ambiente) ou em .streamlit/secrets.toml
    python scripts/resumo_diario.py --seller "Web Continental"
    python scripts/resumo_diario.py --seller "Web Continental" --seller "Outro" --saida resumos/
    python scripts/resumo_diario.py --demo          # dados sintéticos, sem banco

Mesmas regras do painel (`streamlit_app.py`), repetidas aqui de propósito
porque aquele arquivo é um script do Streamlit e não se importa:

  * só `superficie = 'marketplace'` entra — loja própria o lojista joga sozinho;
  * oferta com `identidade_suspeita` fica fora de todo número;
  * turno não coletado não vira zero: vira um aviso no rodapé do resumo.

O que o resumo NÃO afirma: a base só tem a linha de quem DETÉM a buy box, então
não existe "segundo menor preço". O preço-alvo sai do preço do rival que levou
a caixa; "você está sozinho" sai de `qtd_sellers = 1`. Nada aqui é custo ou
margem do seller.
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

TURNO_ORDEM = {"abertura": 1, "tarde": 2, "fechamento": 3}
TURNO_HORA = {1: 8, 2: 14, 3: 20}
SAUDACAO = {1: "Bom dia", 2: "Boa tarde", 3: "Boa noite"}

COLUNAS_FATO = ["data", "turno", "plataforma", "superficie", "offer_key",
                "marketplace_product_id", "produto", "marca", "preco",
                "posicao_melhor", "posicao_mediana", "detentor_buybox",
                "detentor_anterior", "virou_no_turno", "qtd_sellers",
                "identidade_suspeita"]
COLUNAS_PERDIDOS = ["data", "turno", "plataforma", "seller_canonical",
                    "marketplace_product_id", "offer_key", "produto", "marca", "preco"]
COLUNAS_SHARE = ["data", "plataforma", "seller_canonical", "produtos_detidos",
                 "produtos_universo", "share_buybox_pct"]
COLUNAS_COBERTURA = ["data", "turno", "plataforma", "observado"]

_NUMERICAS = {"preco", "posicao_melhor", "posicao_mediana", "qtd_sellers",
              "produtos_detidos", "produtos_universo", "share_buybox_pct"}
_BOOLEANAS = {"detentor_buybox", "virou_no_turno", "identidade_suspeita", "observado"}

# Quantos dias para trás ler: cobre o "dia observado anterior" do share mesmo
# com um fim de semana sem coleta no meio, e o histórico de "sozinho há N turnos".
JANELA_DIAS = 7
PIORA_BUSCA_MIN = 5       # posições, mesma régua do alerta do painel
TURNOS_SOZINHO_MIN = 3    # turnos seguidos como único vendedor

#: Tipos de ação, na ordem de prioridade do resumo. O rótulo é o que a aba do
#: painel mostra no filtro.
TIPOS_ACAO = {"perda": "Buy box perdida", "busca": "Queda na busca",
              "sozinho": "Único vendedor no anúncio"}


# ── Leitura ──────────────────────────────────────────────────────────────────
@dataclass
class Dados:
    fato: pd.DataFrame
    perdidos: pd.DataFrame
    share: pd.DataFrame
    cobertura: pd.DataFrame


def _tipar(df: pd.DataFrame) -> pd.DataFrame:
    """psycopg2 entrega `Decimal` e `date` nativos; aqui vira float, datetime e
    boolean anulável (NA = não observado, nunca False)."""
    for col in _NUMERICAS & set(df.columns):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    for col in _BOOLEANAS & set(df.columns):
        df[col] = df[col].astype("boolean")
    if "data" in df.columns:
        df["data"] = pd.to_datetime(df["data"], errors="coerce")
    return df


def _sim(serie: pd.Series) -> np.ndarray:
    return serie.astype("boolean").fillna(False).to_numpy(dtype=bool)


def _momento(df: pd.DataFrame) -> pd.Series:
    """data + hora do turno — ordena e mede "últimas 24h"."""
    ordem = df["turno"].astype("string").str.strip().str.lower().map(TURNO_ORDEM)
    horas = ordem.map(TURNO_HORA).fillna(0).astype(int)
    return df["data"] + pd.to_timedelta(horas, unit="h")


def dsn_configurado() -> str | None:
    """`RAC_DB_DSN` do ambiente, ou do mesmo secrets.toml que o painel usa."""
    dsn = os.environ.get("RAC_DB_DSN", "").strip()
    if dsn:
        return dsn
    segredos = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    if segredos.exists():
        with segredos.open("rb") as f:
            return (tomllib.load(f).get("RAC_DB_DSN") or "").strip() or None
    return None


def _consultar(conn, sql: str, params: list, colunas: list[str]) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        linhas = cur.fetchall()
    return _tipar(pd.DataFrame(linhas, columns=colunas))


def carregar(conn, seller: str, hoje: date) -> Dados:
    desde = (hoje - timedelta(days=JANELA_DIAS)).isoformat()
    ate = hoje.isoformat()

    fato = _consultar(conn,
        f"SELECT {', '.join(COLUNAS_FATO)} FROM seller_offer_daily "
        "WHERE seller_canonical = %s AND data >= %s AND data <= %s",
        [seller, desde, ate], COLUNAS_FATO)

    # Perda = linha de OUTRO seller cujo detentor_anterior é este.
    perdidos = _consultar(conn,
        f"SELECT {', '.join(COLUNAS_PERDIDOS)} FROM seller_offer_daily "
        "WHERE detentor_anterior = %s AND virou_no_turno AND NOT identidade_suspeita "
        "AND superficie = 'marketplace' AND data >= %s AND data <= %s",
        [seller, desde, ate], COLUNAS_PERDIDOS)

    # A view é lida com `data = X`, um dia por vez: com `>=` o Postgres não
    # propaga o filtro para o lado "universo" do join e estoura o timeout
    # (mesmo motivo de `_share_por_dia` no painel).
    blocos = []
    dia = hoje - timedelta(days=JANELA_DIAS)
    while dia <= hoje:
        blocos.append(_consultar(conn,
            f"SELECT {', '.join(COLUNAS_SHARE)} FROM v_seller_buybox_share "
            "WHERE data = %s AND seller_canonical = %s",
            [dia.isoformat(), seller], COLUNAS_SHARE))
        dia += timedelta(days=1)
    share = pd.concat(blocos, ignore_index=True)

    cobertura = _consultar(conn,
        f"SELECT {', '.join(COLUNAS_COBERTURA)} FROM seller_coverage_daily "
        "WHERE data >= %s AND data <= %s",
        [desde, ate], COLUNAS_COBERTURA)

    return Dados(fato, perdidos, share, cobertura)


# ── Análise ──────────────────────────────────────────────────────────────────
@dataclass
class Acao:
    tipo: str        # "perda" | "busca" | "sozinho"
    texto: str
    peso: float      # ordena dentro do tipo: preço do produto em jogo


@dataclass
class Resumo:
    seller: str
    referencia: pd.Timestamp | None
    share_total: tuple[float, float | None] | None = None
    share_plataformas: list[tuple[str, float, float | None]] = field(default_factory=list)
    ganhos: int = 0
    perdas: int = 0
    rival_top: tuple[str, int] | None = None
    acoes: list[Acao] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


def _brl(valor: float) -> str:
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def _pts(valor: float) -> str:
    return f"{valor:+.1f}".replace(".", ",").replace("-", "−")


def _pct(valor: float) -> str:
    return f"{valor:.1f}%".replace(".", ",")


def _limpo(fato: pd.DataFrame) -> pd.DataFrame:
    return fato[(fato["superficie"] == "marketplace").to_numpy(dtype=bool)
                & ~_sim(fato["identidade_suspeita"])]


def _share(share: pd.DataFrame) -> tuple[tuple[float, float | None] | None,
                                         list[tuple[str, float, float | None]]]:
    """Último dia observado de cada plataforma × o observado antes dele —
    a mesma régua do KPI do painel."""
    if share.empty:
        return None, []
    por_plat = []
    det = uni = det_ant = uni_ant = 0.0
    tem_anterior = False
    for plataforma, bloco in share.sort_values("data").groupby("plataforma"):
        dias = bloco.groupby("data")[["produtos_detidos", "produtos_universo"]].sum()
        d, u = dias.iloc[-1]
        det, uni = det + d, uni + u
        pct = 100 * d / u if u else 0.0
        delta = None
        if len(dias) >= 2:
            da, ua = dias.iloc[-2]
            det_ant, uni_ant = det_ant + da, uni_ant + ua
            tem_anterior = True
            delta = pct - (100 * da / ua if ua else 0.0)
        por_plat.append((plataforma, pct, delta))
    total = 100 * det / uni if uni else 0.0
    delta_total = (total - 100 * det_ant / uni_ant) if tem_anterior and uni_ant else None
    por_plat.sort(key=lambda x: -x[1])
    return (total, delta_total), por_plat


def _acoes_perda(perdas: pd.DataFrame, limpo: pd.DataFrame) -> list[Acao]:
    """Uma ação por produto perdido que ainda não foi retomado."""
    acoes = []
    if perdas.empty:
        return acoes
    meus = limpo[_sim(limpo["detentor_buybox"])].assign(_t=lambda d: _momento(d))
    ultimas = (perdas.sort_values("_t")
               .drop_duplicates(["plataforma", "marketplace_product_id"], keep="last"))
    for _, p in ultimas.iterrows():
        mesmo = meus[(meus["plataforma"] == p["plataforma"])
                     & (meus["marketplace_product_id"] == p["marketplace_product_id"])]
        if (mesmo["_t"] > p["_t"]).any():
            continue  # já retomou depois da perda
        antes = mesmo[mesmo["_t"] < p["_t"]].sort_values("_t")
        meu_preco = antes["preco"].iloc[-1] if not antes.empty else np.nan
        rival = p["preco"]
        texto = f"*{p['produto']}* ({p['plataforma']}): {p['seller_canonical']} levou a buy box"
        if pd.notna(rival) and pd.notna(meu_preco) and meu_preco <= rival:
            # Perdeu sem estar mais caro: preço não é a alavanca, e sugerir
            # baixar mais seria empurrar margem fora sem motivo.
            texto += (f" a {_brl(rival)}, mas seu preço já era {_brl(meu_preco)}. "
                      "Confira frete, prazo e reputação.")
        elif pd.notna(rival):
            texto += f" a {_brl(rival)}."
            if pd.notna(meu_preco):
                texto += (f" Seu último preço com ela: {_brl(meu_preco)} "
                          f"({_brl(meu_preco - rival)} acima).")
            texto += f" Para retomar: *{_brl(np.floor(rival) - 1)}*."
        else:
            texto += "."
        acoes.append(Acao("perda", texto, float(rival) if pd.notna(rival) else 0.0))
    return acoes


def _acoes_busca(limpo: pd.DataFrame) -> list[Acao]:
    """Produto que caiu na busca entre os dois últimos dias observados."""
    base = limpo.dropna(subset=["posicao_melhor", "marketplace_product_id"])
    if base.empty:
        return []
    dias = sorted(base["data"].unique())
    if len(dias) < 2:
        return []
    chave = ["plataforma", "marketplace_product_id"]
    ontem = base[base["data"] == dias[-2]].groupby(chave)["posicao_melhor"].min()
    hoje = base[base["data"] == dias[-1]].groupby(chave)["posicao_melhor"].min()
    piora = (hoje - ontem).dropna()
    piora = piora[piora >= PIORA_BUSCA_MIN]
    nomes = base.drop_duplicates(chave, keep="last").set_index(chave)
    acoes = []
    for idx, pos in piora.items():
        info = nomes.loc[idx]
        acoes.append(Acao("busca",
            f"*{info['produto']}* ({idx[0]}): caiu da posição {ontem[idx]:.0f} "
            f"para {hoje[idx]:.0f} na busca. Revise título, frete e preço.",
            float(info["preco"]) if pd.notna(info["preco"]) else 0.0))
    return acoes


def _acoes_sozinho(limpo: pd.DataFrame) -> list[Acao]:
    """Produto em que você é o único vendedor há vários turnos: espaço para
    testar preço. Só os que você detém no último turno da plataforma."""
    meus = limpo[_sim(limpo["detentor_buybox"])].dropna(subset=["marketplace_product_id"])
    if meus.empty:
        return []
    meus = meus.assign(_t=_momento(meus))
    ultimo = meus.groupby("plataforma")["_t"].transform("max")
    atuais = meus[(meus["_t"] == ultimo) & (meus["qtd_sellers"] == 1)]
    acoes = []
    for _, linha in atuais.iterrows():
        hist = meus[(meus["plataforma"] == linha["plataforma"])
                    & (meus["marketplace_product_id"] == linha["marketplace_product_id"])
                    ].sort_values("_t", ascending=False)
        seguidos = 0
        for qtd in hist["qtd_sellers"]:
            if qtd != 1:
                break
            seguidos += 1
        if seguidos >= TURNOS_SOZINHO_MIN:
            acoes.append(Acao("sozinho",
                f"*{linha['produto']}* ({linha['plataforma']}): você é o único vendedor "
                f"há {seguidos} turnos, a {_brl(linha['preco'])}. Dá para testar um "
                "preço um pouco maior.",
                float(linha["preco"]) if pd.notna(linha["preco"]) else 0.0))
    return acoes


def _escolher(acoes: list[Acao], n: int) -> list[Acao]:
    """Uma de cada tipo primeiro (perda > busca > sozinho), depois completa com
    as perdas de maior valor. Três perdas iguais dizem menos que três frentes."""
    prioridade = list(TIPOS_ACAO)
    por_tipo = {t: sorted([a for a in acoes if a.tipo == t], key=lambda a: -a.peso)
                for t in prioridade}
    escolhidas = [por_tipo[t][0] for t in prioridade if por_tipo[t]][:n]
    resto = [a for t in prioridade for a in por_tipo[t] if a not in escolhidas]
    escolhidas += resto[:max(0, n - len(escolhidas))]
    return sorted(escolhidas, key=lambda a: prioridade.index(a.tipo))


def _avisos_cobertura(cobertura: pd.DataFrame, plataformas: set[str],
                      dia: pd.Timestamp) -> list[str]:
    if cobertura.empty or not plataformas:
        return []
    cob = cobertura[(cobertura["data"] == dia) & cobertura["plataforma"].isin(plataformas)]
    faltas = cob[~_sim(cob["observado"])]
    avisos = []
    for _, f in faltas.sort_values(["plataforma", "turno"]).iterrows():
        avisos.append(f"{f['plataforma']}: turno {f['turno']} não foi coletado em "
                      f"{dia:%d/%m} e ficou fora dos números.")
    return avisos


def montar_resumo(seller: str, dados: Dados, n_acoes: int = 3,
                  tipos: set[str] | None = None) -> Resumo:
    """`tipos` restringe as ações a um subconjunto de `TIPOS_ACAO`; None = todas."""
    limpo = _limpo(dados.fato)
    perdidos = dados.perdidos.copy()
    momentos = pd.concat([_momento(limpo), _momento(perdidos)]).dropna()
    if momentos.empty:
        return Resumo(seller, None, avisos=["Sem oferta registrada na última semana."])
    fim = momentos.max()
    inicio = fim - pd.Timedelta(hours=24)

    ganhos = limpo[_sim(limpo["virou_no_turno"]) & (_momento(limpo) > inicio).to_numpy()]
    perdidos = perdidos.assign(_t=_momento(perdidos))
    perdas = perdidos[perdidos["_t"] > inicio]

    total, por_plat = _share(dados.share)
    rival = None
    if not perdas.empty:
        top = perdas["seller_canonical"].value_counts()
        rival = (top.index[0], int(top.iloc[0]))

    acoes = _acoes_perda(perdas, limpo) + _acoes_busca(limpo) + _acoes_sozinho(limpo)
    if tipos is not None:
        acoes = [a for a in acoes if a.tipo in tipos]
    plataformas = set(limpo["plataforma"].dropna()) | set(perdidos["plataforma"].dropna())
    return Resumo(
        seller=seller, referencia=fim, share_total=total, share_plataformas=por_plat,
        ganhos=len(ganhos), perdas=len(perdas), rival_top=rival,
        acoes=_escolher(acoes, n_acoes),
        avisos=_avisos_cobertura(dados.cobertura, plataformas, fim.normalize()),
    )


# ── Texto ────────────────────────────────────────────────────────────────────
def formatar(r: Resumo, por_plataforma: bool = True) -> str:
    """Texto com a marcação do WhatsApp (*negrito*, _itálico_)."""
    if r.referencia is None:
        return f"*{r.seller}*\n" + "\n".join(r.avisos)
    ordem = {v: k for k, v in TURNO_HORA.items()}.get(r.referencia.hour, 1)
    linhas = [f"*{SAUDACAO[ordem]}, {r.seller}*",
              f"Leitura das {r.referencia:%Hh} de {r.referencia:%d/%m}, últimas 24h", ""]
    if r.share_total:
        pct, delta = r.share_total
        linhas.append(f"*Buy box: {_pct(pct)}*"
                      + (f" ({_pts(delta)} pts)" if delta is not None else ""))
        for plataforma, p, d in (r.share_plataformas if por_plataforma else []):
            linhas.append(f"• {plataforma}: {_pct(p)}" + (f" ({_pts(d)})" if d is not None else ""))
    linhas.append(f"• Ganhou {r.ganhos} · Perdeu {r.perdas}")
    if r.rival_top:
        linhas.append(f"• Quem mais te tirou: *{r.rival_top[0]}* ({r.rival_top[1]})")
    if r.acoes:
        rotulo = "ação" if len(r.acoes) == 1 else "ações"
        linhas += ["", f"*{len(r.acoes)} {rotulo} para hoje*"]
        linhas += [f"{i}. {a.texto}" for i, a in enumerate(r.acoes, 1)]
    else:
        linhas += ["", "Nenhuma ação urgente hoje. Buy box estável."]
    if r.avisos:
        linhas += [""] + [f"_⚠️ {a}_" for a in r.avisos]
    linhas += ["", "_Preços observados na vitrine pública dos marketplaces._"]
    return "\n".join(linhas)


# ── Dados sintéticos (--demo) ────────────────────────────────────────────────
def dados_demo(seller: str = "Loja Exemplo", hoje: date | None = None, semente: int = 7) -> Dados:
    """Mercado fictício com a mesma forma das tabelas reais: 2 plataformas,
    12 produtos, 5 sellers, 3 turnos por dia. Serve para testar o texto sem banco."""
    rng = np.random.default_rng(semente)
    hoje = hoje or date.today()
    sellers = [seller, "Rival A", "Rival B", "Rival C", "Rival D"]
    produtos = [(f"Split Inverter {btu} BTUs {marca}", marca, base)
                for btu, base in ((9000, 1899), (12000, 2349), (18000, 3299), (24000, 4199))
                for marca in ("Marca A", "Marca B", "Marca C")]
    turnos = ["Abertura", "Tarde", "Fechamento"]
    linhas = []
    for plataforma in ("Mercado Livre", "Magalu"):
        for i, (nome, marca, base) in enumerate(produtos):
            pid = f"{plataforma[:2].upper()}{1000 + i}"
            dono = sellers[rng.integers(len(sellers))]
            pos = int(rng.integers(1, 15))
            sozinho = rng.random() < 0.15
            for d in range(JANELA_DIAS, -1, -1):
                dia = pd.Timestamp(hoje - timedelta(days=d))
                for turno in turnos:
                    anterior = dono
                    if rng.random() < 0.18:
                        dono = sellers[rng.integers(len(sellers))]
                    pos = int(np.clip(pos + rng.integers(-2, 3), 1, 40))
                    linhas.append(dict(
                        data=dia, turno=turno, plataforma=plataforma, superficie="marketplace",
                        offer_key=f"{pid}-{dono}", marketplace_product_id=pid, produto=nome,
                        marca=marca, preco=round(base * (1 + rng.normal(0, 0.02)), 2),
                        posicao_melhor=float(pos), posicao_mediana=float(pos + 2),
                        detentor_buybox=True,
                        detentor_anterior=anterior if anterior != dono else None,
                        virou_no_turno=anterior != dono,
                        qtd_sellers=1.0 if sozinho else float(rng.integers(2, 7)),
                        identidade_suspeita=False, seller_canonical=dono))
    # Força os dois casos menos frequentes, para o exemplo mostrar todo tipo
    # de ação: uma queda na busca e um produto em que o seller está sozinho.
    mercado = _tipar(pd.DataFrame(linhas))
    ultimo = mercado["data"].max()
    meus = mercado[mercado["seller_canonical"] == seller]
    nos_dois = (meus[meus["data"] >= ultimo - pd.Timedelta(days=1)]
                .groupby("marketplace_product_id")["data"].nunique())
    nos_dois = nos_dois[nos_dois == 2].index
    if len(nos_dois):
        mercado.loc[(mercado["marketplace_product_id"] == nos_dois[0])
                    & (mercado["data"] == ultimo), "posicao_melhor"] += 9
    if len(nos_dois) > 1:
        mercado.loc[(mercado["marketplace_product_id"] == nos_dois[-1])
                    & (mercado["seller_canonical"] == seller), "qtd_sellers"] = 1.0

    fato = mercado[mercado["seller_canonical"] == seller][COLUNAS_FATO].reset_index(drop=True)
    perdidos = mercado[(mercado["detentor_anterior"] == seller)
                       & _sim(mercado["virou_no_turno"])][COLUNAS_PERDIDOS].reset_index(drop=True)
    universo = mercado.groupby(["data", "plataforma"])["marketplace_product_id"].nunique()
    detidos = (mercado[mercado["seller_canonical"] == seller]
               .groupby(["data", "plataforma"])["marketplace_product_id"].nunique())
    share = (pd.DataFrame({"produtos_detidos": detidos, "produtos_universo": universo})
             .dropna().reset_index().assign(seller_canonical=seller))
    share["share_buybox_pct"] = 100 * share["produtos_detidos"] / share["produtos_universo"]
    cobertura = (mercado[["data", "turno", "plataforma"]].drop_duplicates()
                 .assign(observado=True).reset_index(drop=True))
    cobertura.loc[(cobertura["data"] == ultimo) & (cobertura["plataforma"] == "Magalu")
                  & (cobertura["turno"] == "Tarde"), "observado"] = False
    cobertura["observado"] = cobertura["observado"].astype("boolean")
    return Dados(fato, perdidos, share[COLUNAS_SHARE], cobertura)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resumo diário de buy box por seller, para WhatsApp.")
    ap.add_argument("--seller", action="append", default=[],
                    help="Nome exato em seller_canonical. Repita para vários sellers.")
    ap.add_argument("--data", type=date.fromisoformat, default=date.today(),
                    help="Dia de referência (AAAA-MM-DD). Padrão: hoje.")
    ap.add_argument("--acoes", type=int, default=3, help="Máximo de ações no resumo (padrão 3).")
    ap.add_argument("--saida", type=Path, help="Pasta para gravar um .txt por seller.")
    ap.add_argument("--demo", action="store_true", help="Usa dados sintéticos, sem banco.")
    args = ap.parse_args(argv)

    if args.demo:
        textos = {"Loja Exemplo": formatar(montar_resumo(
            "Loja Exemplo", dados_demo(hoje=args.data), args.acoes))}
    else:
        if not args.seller:
            ap.error("informe --seller (ou use --demo)")
        dsn = dsn_configurado()
        if not dsn:
            ap.error("defina RAC_DB_DSN no ambiente ou em .streamlit/secrets.toml")
        import psycopg2  # só aqui: o --demo roda sem o driver instalado
        textos = {}
        with psycopg2.connect(dsn, connect_timeout=15) as conn:
            conn.set_session(readonly=True, autocommit=True)
            for seller in args.seller:
                dados = carregar(conn, seller, args.data)
                textos[seller] = formatar(montar_resumo(seller, dados, args.acoes))

    for seller, texto in textos.items():
        if args.saida:
            args.saida.mkdir(parents=True, exist_ok=True)
            arquivo = args.saida / f"{args.data:%Y-%m-%d}_{seller.replace(' ', '_')}.txt"
            arquivo.write_text(texto + "\n", encoding="utf-8")
            print(f"gravado: {arquivo}", file=sys.stderr)
        else:
            print(texto, end="\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
