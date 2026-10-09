"""
streamlit_app.py — Track Position Seller, painel do lojista.

REFACTOR COMPLETO (Jan/2025): Todas as melhorias de código e visualização
implementadas conforme documentação de arquitetura.

MUDANÇAS PRINCIPAIS:
  • Configuração centralizada em dataclass
  • Exceções específicas para debugging
  • Type hints com TypedDict para schema enforcement
  • Gráficos Plotly interativos com annotations
  • KPI cards com deltas temporais
  • Tabelas com formatação condicional
  • Calendar heatmap para cobertura
  • Detecção automática de anomalias
  • Lazy loading por aba
  • Validação de colunas contra SQL injection
  • Melhorias de acessibilidade (contraste, tooltips)

REVISÃO (Set/2026):
  • `data` tipada na borda (`_tipar`) — o gráfico de ganhos e perdas saía
    todo zerado porque agrupava texto e reindexava por Timestamp
  • Ganhos e perdas: série diária/semanal (dia sem coleta fica vazio, não
    zero), placar por rival, preço na perda, quebra por turno/plataforma/marca
  • Marcas e posição: foto de um dia, comparação A → B com o que entrou e
    saiu, e evolução do mix de marcas
  • Ranking: sua posição dia a dia e a distância para o líder
  • Filtro de plataforma único na sidebar, valendo para todas as abas
  • Paleta validada para daltonismo, cor fixa por plataforma, sem eixo duplo

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
Streamlit Cloud) — dois bancos possíveis, a fronteira é qual secret existe:

    RAC_DB_DSN = "postgresql://seller_ro:<senha>@<host>.aivencloud.com:<porta>/defaultdb?sslmode=require"
                                      # preferencial (Set/2026): Postgres na
                                      # Aiven, usuário `seller_ro` — só SELECT
                                      # em seller_offer_daily, seller_coverage_
                                      # daily e v_seller_buybox_share (ver
                                      # docs/MIGRACAO_AIVEN.md do
                                      # RAC-Position-tracker). Presente, manda
                                      # tudo para lá — mesma regra do resto do
                                      # projeto (`RAC_DB_DSN` como chave de
                                      # virada, não um `if` espalhado).

    SUPABASE_URL = "https://<projeto>.supabase.co"       # legado — só usado
    SUPABASE_ANON_KEY = "<chave anon — NUNCA a service_role>"  # se RAC_DB_DSN
                                      # estiver ausente (banco voltou a caber
                                      # na cota do free tier).

    SELLER = "Web Continental"       # opcional: trava o painel num seller só.
                                      # Ausente = seletor livre entre todos os
                                      # sellers com dado na janela (uso: demo
                                      # compartilhada). Presente = pensado para
                                      # uma instância dedicada por tenant.
    SENHA = "<senha da demo>"        # opcional; sem ela o painel fica aberto
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, TypedDict

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# ── Logging estruturado ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Track Position Seller",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ── Configuração Centralizada ────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    """Constantes da aplicação — nada mágico espalhado no código."""
    
    # Paginação e cache
    PAGINA_SIZE: int = 1000
    CACHE_TTL: int = 900  # 15 minutos

    # Janela temporal
    WINDOW_MIN: int = 3
    WINDOW_MAX: int = 60
    WINDOW_DEFAULT: int = 14
    
    # Visualização
    TOP_RANKING_DISPLAY: int = 8
    CHART_HEIGHT_SHARE: int = 350
    CHART_HEIGHT_POSICAO: int = 300
    CHART_HEIGHT_MARCA: int = 300
    
    # Cores (WCAG AA compliant - contraste ≥ 4.5:1 em fundo branco)
    class Colors:
        PRIMARY = "#1B6E6A"      # Verde-petróleo (contraste: 5.8:1) ✅
        WARNING = "#8B4513"      # Âmbar escuro (contraste: 6.2:1) ✅
        NEUTRAL = "#56635F"      # Cinza-esverdeado (contraste: 5.8:1) ✅
        SUCCESS = "#28a745"      # Verde sucesso
        DANGER = "#dc3545"       # Vermelho perigo
        INFO = "#17a2b8"         # Azul informação
        LIGHT_BG = "#f8f9fa"     # Fundo claro
        
        @classmethod
        def palette(cls) -> dict[str, str]:
            return {
                "primary": cls.PRIMARY,
                "warning": cls.WARNING,
                "neutral": cls.NEUTRAL,
                "success": cls.SUCCESS,
                "danger": cls.DANGER,
            }

        # Paleta categórica dos gráficos — oito matizes numa ordem FIXA,
        # validada (skill dataviz, `validate_palette.js`) contra as duas
        # superfícies do Streamlit: separação para daltonismo ΔE ≥ 8 entre
        # vizinhos e contraste ≥ 3:1 no tema escuro. No claro, três slots
        # ficam abaixo de 3:1 — por isso todo gráfico tem tabela gêmea.
        # Um passo por tema, mesma ordem: a cor muda de tom, nunca de dono.
        CATEGORICA = {
            "light": ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                      "#e87ba4", "#008300", "#4a3aa7", "#e34948"),
            "dark": ("#3987e5", "#d95926", "#199e70", "#c98500",
                     "#d55181", "#008300", "#9085e9", "#e66767"),
        }
        # Ganho × perda é POLARIDADE, não identidade: par divergente frio ↔
        # quente. Verde × vermelho (o par antigo) é justamente o que o
        # daltônico protan/deutan não separa.
        GANHO = {"light": "#2a78d6", "dark": "#3987e5"}
        PERDA = {"light": "#e34948", "dark": "#e66767"}
        TINTA = {"light": "#52514e", "dark": "#c3c2b7"}   # série derivada
        OUTRAS = "#898781"                                 # cauda / sem dono


# Cor por PLATAFORMA, fixa: a mesma plataforma tem a mesma cor em toda aba e
# em qualquer filtro (cor segue a entidade, nunca a posição na lista). A ordem
# é a dos slots validados; Magalu azul, Amazon laranja e Mercado Livre amarelo
# caem perto da cor da própria marca. Plataforma fora da lista vai para cinza.
PLATAFORMAS_COR = ("Magalu", "Amazon", "Leroy Merlin", "Mercado Livre",
                   "Casas Bahia", "Shopee", "Google Shopping")

# Ordem CRONOLÓGICA do turno — espelho de `turno_ordinal()` da migração 017.
# Alfabeticamente 'Abertura' < 'Fechamento' < 'Tarde'; ordenar pelo texto
# põe o fechamento das 20h antes da tarde das 14h.
TURNO_ORDEM = {"abertura": 1, "tarde": 2, "fechamento": 3}
TURNO_HORA = {1: 8, 2: 14, 3: 20}


# ── Type Hints para Schema Enforcement ───────────────────────────────────────
class OfferRow(TypedDict, total=False):
    """Schema esperado para seller_offer_daily."""
    data: str
    turno: str
    plataforma: str
    superficie: str
    offer_key: str
    marketplace_product_id: str | None
    produto: str
    marca: str
    preco: str
    posicao_melhor: str | None
    posicao_mediana: str | None
    keywords_presente: str | None
    detentor_buybox: bool | None
    detentor_anterior: str | None
    virou_no_turno: bool | None
    qtd_sellers: str | None
    tipo_seller: str | None
    identidade_suspeita: bool | None
    seller_canonical: str


class ShareRow(TypedDict, total=False):
    """Schema esperado para v_seller_buybox_share."""
    data: str
    plataforma: str
    seller_canonical: str
    produtos_detidos: str
    produtos_universo: str
    share_buybox_pct: str


class CoverageRow(TypedDict, total=False):
    """Schema esperado para seller_coverage_daily."""
    data: str
    turno: str
    plataforma: str
    observado: bool
    linhas: int


# ── Exceções Específicas ─────────────────────────────────────────────────────
class DatabaseConnectionError(Exception):
    """Erro de conexão com o banco de dados."""
    pass


class DataFetchError(Exception):
    """Erro ao buscar dados do banco."""
    pass


class ValidationError(Exception):
    """Erro de validação de dados ou parâmetros."""
    pass


# ── Adaptador Postgres (Aiven) ───────────────────────────────────────────────
#
# Em Set/2026 a cota do free tier do Supabase (500 MB) estourou e o PostgREST
# — a API REST que o `supabase-py` consome — passou a devolver HTTP 402
# (`exceed_db_size_quota`) em toda leitura, mesmo com o Postgres saudável por
# trás. O RAC Position Tracker migrou a janela quente para um Postgres na
# Aiven (`utils/db.py`, `docs/MIGRACAO_AIVEN.md`) por trás de um adaptador que
# imita a API fluente do `supabase-py` sobre psycopg2 — assim as chamadas
# `.table().select()...execute()` não mudam, só o transporte.
#
# Esta classe é o mesmo adaptador, recortado para o subconjunto que este
# painel usa (`select/eq/gte/order/range/execute`, só leitura) — o painel é um
# arquivo único de propósito (deploy no Streamlit Community Cloud), então
# importar `utils.db` do repositório principal não é opção.
try:
    import psycopg2
    from psycopg2 import sql as _sql
    from psycopg2.extensions import new_type, register_type
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover - dependência declarada no pyproject
    psycopg2 = None

_OID_NUMERIC, _OID_DATE = 1700, 1082
_OID_TIMESTAMP, _OID_TIMESTAMPTZ = 1114, 1184


def _texto_cru(valor, cur):
    return valor


def _texto_iso(valor, cur):
    return valor.replace(" ", "T", 1) if valor is not None else None


def _registrar_tipos_postgrest(conn) -> None:
    """Faz esta conexão devolver `numeric`/`date`/`timestamp` como texto —
    exatamente o que o PostgREST entrega. Sem isto o psycopg2 devolveria
    `Decimal`/`date` nativos e `_tipar()` (que assume string vinda do
    PostgREST) divergiria em silêncio conforme o backend por trás."""
    register_type(new_type((_OID_NUMERIC,), "RAC_NUMERIC_TEXTO", _texto_cru), conn)
    register_type(new_type((_OID_DATE,), "RAC_DATE_TEXTO", _texto_cru), conn)
    register_type(
        new_type((_OID_TIMESTAMP, _OID_TIMESTAMPTZ), "RAC_TS_TEXTO", _texto_iso), conn)


@dataclass
class _DBResponse:
    data: list = field(default_factory=list)


_OPS = {"eq": "=", "gte": ">="}


# ── Validação de Colunas (SQL Injection Prevention) ─────────────────────────
ALLOWED_COLUMNS: dict[str, set[str]] = {
    "seller_offer_daily": {
        "data", "turno", "plataforma", "superficie", "offer_key",
        "marketplace_product_id", "produto", "marca", "preco",
        "posicao_melhor", "posicao_mediana", "keywords_presente",
        "detentor_buybox", "detentor_anterior", "virou_no_turno",
        "qtd_sellers", "tipo_seller", "identidade_suspeita",
        "seller_canonical",
    },
    "seller_coverage_daily": {
        "data", "turno", "plataforma", "observado", "linhas",
        "seller_canonical",
    },
    "v_seller_buybox_share": {
        "data", "plataforma", "seller_canonical", "produtos_detidos",
        "produtos_universo", "share_buybox_pct",
    },
}


def _validate_columns(table: str, columns: str) -> bool:
    """Valida colunas contra whitelist para prevenir SQL injection."""
    if not columns or columns.strip() == "*":
        return True
    cols = [c.strip() for c in columns.split(",")]
    allowed = ALLOWED_COLUMNS.get(table, set())
    invalid = [c for c in cols if c and c not in allowed]
    if invalid:
        logger.warning(f"Colunas não permitidas para {table}: {invalid}")
        return False
    return True


class _PostgresQuery:
    """Réplica mínima da API fluente do `supabase-py` sobre SQL puro."""

    def __init__(self, client: "_PostgresClient", table: str) -> None:
        self._client = client
        self._table = table
        self._columns = "*"
        self._where: list[Any] = []
        self._params: list[Any] = []
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._offset: int | None = None

    def select(self, columns: str = "*") -> "_PostgresQuery":
        if not _validate_columns(self._table, columns):
            raise ValidationError(f"Colunas inválidas para tabela {self._table}")
        self._columns = columns or "*"
        return self

    def _cmp(self, op: str, column: str, value: Any) -> "_PostgresQuery":
        if column not in ALLOWED_COLUMNS.get(self._table, set()) and column != "*":
            raise ValidationError(f"Coluna {column} não permitida em WHERE")
        self._where.append(
            _sql.SQL("{} {} %s").format(_sql.Identifier(column), _sql.SQL(_OPS[op])))
        self._params.append(value)
        return self

    def eq(self, column: str, value: Any) -> "_PostgresQuery":
        return self._cmp("eq", column, value)

    def gte(self, column: str, value: Any) -> "_PostgresQuery":
        return self._cmp("gte", column, value)

    def order(self, column: str, desc: bool = False) -> "_PostgresQuery":
        if column not in ALLOWED_COLUMNS.get(self._table, set()):
            raise ValidationError(f"Coluna {column} não permitida em ORDER BY")
        self._order.append((column, bool(desc)))
        return self

    def range(self, start: int, end: int) -> "_PostgresQuery":
        """Janela inclusiva nos dois extremos, como no PostgREST."""
        self._offset = int(start)
        self._limit = int(end) - int(start) + 1
        return self

    def _columns_sql(self):
        colunas = self._columns.strip()
        if colunas in ("", "*"):
            return _sql.SQL("*")
        return _sql.SQL(", ").join(
            _sql.Identifier(c.strip()) for c in colunas.split(",") if c.strip())

    def execute(self) -> _DBResponse:
        query = (_sql.SQL("SELECT ") + self._columns_sql()
                 + _sql.SQL(" FROM ") + _sql.Identifier(self._table))
        if self._where:
            query += _sql.SQL(" WHERE ") + _sql.SQL(" AND ").join(self._where)
        if self._order:
            itens = [_sql.SQL("{} {}").format(
                _sql.Identifier(c), _sql.SQL("DESC" if d else "ASC"))
                for c, d in self._order]
            query += _sql.SQL(" ORDER BY ") + _sql.SQL(", ").join(itens)
        params = list(self._params)
        if self._limit is not None:
            query += _sql.SQL(" LIMIT %s")
            params.append(self._limit)
        if self._offset:
            query += _sql.SQL(" OFFSET %s")
            params.append(self._offset)
        return _DBResponse(data=self._client._fetch(query, params))


class _PostgresClient:
    """Uma conexão viva ao Postgres, reconectada sob demanda — mesma forma
    de uso do `Client` do `supabase-py` (`.table(...)`), só leitura."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None
        self._lock = threading.RLock()

    def _connection(self):
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg2.connect(self._dsn, connect_timeout=15)
                self._conn.autocommit = True
                _registrar_tipos_postgrest(self._conn)
                logger.info("Conexão PostgreSQL estabelecida com sucesso")
            except Exception as e:
                logger.error(f"Falha ao conectar no PostgreSQL: {e}")
                raise DatabaseConnectionError(f"Não foi possível conectar ao banco: {e}")
        return self._conn

    def _fetch(self, query, params: list[Any]) -> list[dict]:
        with self._lock:
            for tentativa in (1, 2):
                try:
                    conn = self._connection()
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(query, params)
                        return [dict(r) for r in cur.fetchall()]
                except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
                    logger.warning(f"Tentativa {tentativa} falhou: {e}")
                    self._conn = None
                    if tentativa == 2:
                        logger.error(f"Falha após 2 tentativas: {e}")
                        raise DataFetchError(f"Erro ao buscar dados: {e}")
        return []  # inalcançável — a 2ª tentativa sempre retorna ou levanta

    def table(self, name: str) -> _PostgresQuery:
        if name not in ALLOWED_COLUMNS:
            raise ValidationError(f"Tabela {name} não permitida")
        return _PostgresQuery(self, name)


@st.cache_resource(show_spinner="Inicializando conexão com banco de dados...")
def _client():
    """Backend escolhido pela credencial presente, não por um `if` de app:
    `RAC_DB_DSN` manda para a Aiven; sem ele, segue no Supabase (legado)."""
    dsn = st.secrets.get("RAC_DB_DSN", "").strip()
    if dsn:
        if psycopg2 is None:
            raise DatabaseConnectionError(
                "RAC_DB_DSN definido mas psycopg2 não está instalado "
                "(adicione psycopg2-binary às dependências).")
        cliente = _PostgresClient(dsn)
        try:
            cliente._fetch(_sql.SQL("SELECT 1"), [])  # health check
            return cliente
        except Exception as e:
            logger.error(f"Health check falhou: {e}")
            raise

    try:
        from supabase import create_client
        logger.info("Usando Supabase como backend (legado)")
        return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])
    except KeyError as e:
        raise DatabaseConnectionError(
            f"Credencial {e} não encontrada. Configure RAC_DB_DSN ou SUPABASE_*."
        )


PAGINA = Config.PAGINA_SIZE

# `numeric`/`decimal` do Postgres chega pelo PostgREST como STRING JSON, não
# número — é assim que a API evita perder precisão de ponto flutuante em
# coluna de dinheiro. Sem esta conversão, `share.pivot_table(..., aggfunc=
# "mean")` quebra com "dtype 'str' does not support operation 'mean'" — bug
# real que chegou a ir para produção porque nenhum teste anterior tocava
# dado de verdade (o sandbox de desenvolvimento não alcança o Supabase).
_COLUNAS_NUMERICAS = {"share_buybox_pct", "preco", "posicao_mediana",
                      "posicao_melhor", "qtd_sellers", "keywords_presente",
                      "produtos_detidos", "produtos_universo", "linhas"}

# Booleanos com NULL de verdade (`detentor_buybox` NULL = "plataforma não
# expõe vencedor", `virou_no_turno` NULL = "sem turno anterior observado").
# Com NULL no meio o pandas cria coluna `object`, e aí `~coluna` inverte bit
# de inteiro (~True == -2) e máscara vira índice — daí o dtype `boolean`
# (nullable) na borda, que guarda o NA sem quebrar a lógica booleana.
_COLUNAS_BOOLEANAS = {"detentor_buybox", "virou_no_turno",
                      "identidade_suspeita", "observado"}


def _tipar(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza os tipos do Postgres uma vez, na borda de entrada — para que
    nenhum código adiante precise lembrar disso.

    `data` vira `datetime64`: tanto o PostgREST quanto o adaptador psycopg2
    (ver `_registrar_tipos_postgrest`) entregam `date` como TEXTO
    ('2026-09-01'). Foi isso que zerou o gráfico de ganhos e perdas: a série
    agrupada por texto era reindexada por `pd.date_range` (Timestamps), nenhum
    dia casava e todo dia virava 0 — enquanto o KPI, que só conta linhas,
    mostrava 41 ganhos e 36 perdas na mesma tela.
    """
    for col in _COLUNAS_NUMERICAS & set(df.columns):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in _COLUNAS_BOOLEANAS & set(df.columns):
        df[col] = df[col].astype("boolean")
    if "data" in df.columns:
        df["data"] = pd.to_datetime(df["data"], errors="coerce")
    return df


def _sim(serie: pd.Series) -> np.ndarray:
    """Máscara booleana pura: True só onde o valor É True. NA (não observado)
    vira False — nunca "perdeu", nunca "ganhou"."""
    return serie.astype("boolean").fillna(False).to_numpy(dtype=bool)


def _turno_ord(serie: pd.Series) -> pd.Series:
    return serie.astype("string").str.strip().str.lower().map(TURNO_ORDEM)


def _momento(df: pd.DataFrame) -> pd.Series:
    """Instante da observação (data + hora do turno) — ordena cronologicamente
    e permite casar eventos com a observação imediatamente anterior."""
    horas = _turno_ord(df["turno"]).map(TURNO_HORA).fillna(0).astype(int)
    return df["data"] + pd.to_timedelta(horas, unit="h")


def _fmt_dia(valor) -> str:
    return pd.Timestamp(valor).strftime("%d/%m/%Y") if pd.notna(valor) else "—"


def _tema() -> str:
    """'dark' ou 'light', do tema ativo no navegador do usuário."""
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # pragma: no cover - fora de um script run
        return "dark"


def _cor_plataforma(plataforma: str) -> str:
    if plataforma in PLATAFORMAS_COR:
        return Config.Colors.CATEGORICA[_tema()][PLATAFORMAS_COR.index(plataforma)]
    return Config.Colors.OUTRAS


# As colunas pedidas ao PostgREST, uma vez só: o mesmo texto vai no `select()`
# e no esqueleto do dataframe vazio, para os dois não divergirem em silêncio.
_SELECT_FATO = (
    "data,turno,plataforma,superficie,offer_key,marketplace_product_id,produto,marca,"
    "preco,posicao_melhor,posicao_mediana,keywords_presente,detentor_buybox,"
    "detentor_anterior,virou_no_turno,qtd_sellers,tipo_seller,identidade_suspeita"
)
# `marketplace_product_id` é o que casa a perda com o MEU último preço no
# mesmo produto (aba Ganhos e perdas → "Preço na perda").
# A view e a cobertura são lidas com `select("*")`; o esqueleto abaixo é só
# o que o painel USA — com zero linhas o quadro ainda tem as colunas (ver
# `_quadro`), e o filtro de plataforma não morre em KeyError.
_COLUNAS_SHARE = ("data,plataforma,seller_canonical,produtos_detidos,"
                  "produtos_universo,share_buybox_pct")
_COLUNAS_COBERTURA = "data,turno,plataforma,observado,linhas"
_SELECT_PERDIDOS = ("data,turno,plataforma,seller_canonical,marketplace_product_id,"
                    "produto,marca,preco")


def _quadro(linhas: list[OfferRow], select: str) -> pd.DataFrame:
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


def _todas_as_linhas(fabrica, ordenar_por: list[str]) -> list[dict]:
    """Lê a consulta inteira, página a página.

    `fabrica` é um callable que devolve uma consulta NOVA a cada chamada, não
    a consulta já montada — e isso conserta um bug concreto. O builder do
    supabase-py ACUMULA `offset`/`limit` quando a mesma consulta é paginada
    reusando o objeto: `.range()` faz `params.add(...)`, que anexa em vez de
    substituir. No log de produção via-se a URL crescer a cada página
    (`?...&offset=0&offset=1000&offset=2000...&limit=1000&limit=1000...`), e a
    resposta só saía certa porque o PostgREST, diante de parâmetros repetidos,
    usa o ÚLTIMO. É uma dependência frágil de comportamento não documentado: se
    um dia passar a usar o PRIMEIRO, a paginação relê a primeira página para
    sempre (loop infinito, URL inchando até estourar). Montar uma consulta
    limpa por página elimina a dependência — cada requisição leva um só
    `offset` e um só `limit`.

    O teto de 1.000 linhas por resposta que obriga a paginar é regra do
    PostgREST (Supabase): ele trunca em 1.000 e **não avisa** — um seller com
    2.865 linhas na janela apareceria com um terço do histórico, subestimando
    ofertas, viradas e cobertura sem sinal na tela. A ordenação estável é o que
    garante que as páginas não se sobreponham nem pulem linhas entre chamadas.

    O backend Postgres direto (adaptador Aiven) não tem esse teto — uma só
    `SELECT` traz tudo —, então ali a paginação é dispensável e basta uma
    consulta.
    """
    def _ordenada():
        consulta = fabrica()
        for coluna in ordenar_por:
            consulta = consulta.order(coluna)
        return consulta

    # Postgres direto: sem teto de linhas por resposta, uma consulta basta.
    if isinstance(_ordenada(), _PostgresQuery):
        return _ordenada().execute().data or []

    linhas: list[dict] = []
    inicio = 0
    while True:
        lote = _ordenada().range(inicio, inicio + PAGINA - 1).execute().data or []
        linhas.extend(lote)
        if len(lote) < PAGINA:
            return linhas
        inicio += PAGINA


def _share_por_dia(cli, desde: date, seller: str | None = None) -> list[dict]:
    """Lê `v_seller_buybox_share` UM DIA POR VEZ (`data = X`), nunca `data >= X`.

    A view junta dois agregados sobre `seller_offer_daily` (`detidos` e
    `universo`) por `(data, plataforma)`. Com igualdade o Postgres propaga o
    filtro para os DOIS lados do join e cada dia sai por índice em ~20 ms. Com
    `>=` ele não propaga: o lado `universo` agrega o histórico INTEIRO a cada
    requisição — e a cada página do OFFSET de novo. Em Out/2026, com ~80 mil
    linhas em 14 dias, isso estourou o `statement_timeout` do papel `anon`
    (erro 57014) no Supabase: o seletor de sellers voltava vazio e o painel
    não abria ("Sem sellers com dado na janela selecionada").

    Um dia tem ~200 linhas no mercado todo, abaixo do teto de 1.000 do
    PostgREST, mas a leitura continua paginada por `_todas_as_linhas` caso o
    mercado cresça. `(plataforma, seller_canonical)` é a chave de agrupamento
    da view dentro do dia — ordem única, páginas estáveis.
    """
    linhas: list[dict] = []
    dia = desde
    while dia <= date.today():
        def _consulta(d=dia.isoformat()):
            consulta = cli.table("v_seller_buybox_share").select("*").eq("data", d)
            return consulta.eq("seller_canonical", seller) if seller else consulta
        linhas.extend(_todas_as_linhas(_consulta, ["plataforma", "seller_canonical"]))
        dia += timedelta(days=1)
    return linhas


@st.cache_data(ttl=Config.CACHE_TTL, show_spinner="Carregando dados do mercado...")
def carregar_mercado(desde: date) -> pd.DataFrame:
    """Share de TODOS os sellers na janela, sem filtro por seller.

    Uma única consulta alimenta duas coisas: a lista do seletor (§ picker) e o
    ranking por plataforma (aba Ranking). É dado público — a mesma vitrine que
    qualquer visitante do marketplace vê — então nomear e ordenar concorrentes
    aqui não fura fronteira nenhuma de tenant.
    """
    linhas = _share_por_dia(_client(), desde)
    # Sem try/except aqui de propósito: exceção NÃO entra no cache do
    # `st.cache_data`, dataframe vazio entra — e ficaria 15 min dizendo
    # "sem sellers na janela" depois de uma falha de rede de um segundo.
    return _tipar(_quadro(linhas, _COLUNAS_SHARE))


@st.cache_data(ttl=Config.CACHE_TTL, show_spinner="Carregando dados do seller...")
def carregar(seller: str, desde: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fato do seller, share e cobertura. TTL de 15 min — a coleta é 3x/dia."""
    cli = _client()
    d = desde.isoformat()

    try:
        fato = _tipar(_quadro(_todas_as_linhas(
            lambda: cli.table("seller_offer_daily").select(_SELECT_FATO)
            .eq("seller_canonical", seller).gte("data", d),
            ["data", "turno", "plataforma", "offer_key"]), _SELECT_FATO))

        share = _tipar(_quadro(_share_por_dia(cli, desde, seller), _COLUNAS_SHARE))

        cobertura = _tipar(_quadro(_todas_as_linhas(
            lambda: cli.table("seller_coverage_daily").select("*").gte("data", d),
            ["data", "turno", "plataforma"]), _COLUNAS_COBERTURA))

        return fato, share, cobertura
    except Exception as e:
        logger.error(f"Erro ao carregar dados de {seller}: {e}")
        raise DataFetchError(f"Falha ao carregar dados do seller: {e}")


@st.cache_data(ttl=Config.CACHE_TTL, show_spinner="Carregando produtos perdidos...")
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
        lambda: cli.table("seller_offer_daily").select(_SELECT_PERDIDOS)
        .eq("detentor_anterior", seller).eq("virou_no_turno", True)
        .eq("identidade_suspeita", False).gte("data", desde.isoformat()),
        # A chave primária inteira, para a ordem ser ÚNICA: várias perdas no
        # mesmo (data, turno, plataforma) cruzariam a fronteira de página em
        # ordem instável (ver `carregar_mercado`).
        ["data", "turno", "plataforma", "seller_canonical", "offer_key"])
    # Falha propaga (não entra no cache): devolver vazio aqui mostraria
    # "0 perdidos" por 15 minutos — um número errado com cara de bom.
    return _tipar(_quadro(linhas, _SELECT_PERDIDOS))


# ── Recortes derivados ───────────────────────────────────────────────────────
def _ganhos(limpo: pd.DataFrame) -> pd.DataFrame:
    """Viradas a favor: este seller tomou a BB de outro no turno."""
    return limpo[_sim(limpo["virou_no_turno"])]


def _detidos(limpo: pd.DataFrame) -> pd.DataFrame:
    """Linhas em que este seller detinha a BB. NA fica fora: é plataforma que
    não expõe vencedor (ex.: Casas Bahia), não "perdeu"."""
    return limpo[_sim(limpo["detentor_buybox"])]


def _chave_produto(df: pd.DataFrame) -> pd.Series:
    """Conta PRODUTO (marketplace_product_id), não oferta: a mesma ligação
    produto+marca pode render mais de uma offer_key ao longo da janela (a URL
    canônica muda, ou o produto cai no degrau de hash em vez do de id) —
    contar offer_key infla a marca. Fallback pro offer_key só nas linhas sem
    id de produto."""
    return df["marketplace_product_id"].fillna(df["offer_key"])


def _marca(df: pd.DataFrame) -> pd.Series:
    """`marca` NaN cairia fora do groupby por padrão (dropna=True) e a marca
    desapareceria do gráfico sem aviso nenhum — rotular antes de agrupar
    mantém o produto visível em vez de sumir."""
    return df["marca"].fillna("Sem marca informada")


def _brl(valor) -> str:
    if pd.isna(valor):
        return "—"
    return "R$ " + f"{valor:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _pct(valor, sinal: bool = True) -> str:
    if pd.isna(valor):
        return "—"
    return f"{valor:+.1f}%" if sinal else f"{valor:.1f}%"


def _com_datas(styler, colunas=("Data", "Observado em")):
    """Datas no formato brasileiro na tela, sem perder o valor de data por
    baixo (a ordenação por clique na coluna continua cronológica)."""
    presentes = [c for c in colunas if c in styler.data.columns]
    # `subset` é obrigatório: `Styler.format(dict)` sem ele reaplica o
    # formatador PADRÃO a toda coluna fora do dict — e apagava o "R$" e o
    # "%" que o chamador já tinha formatado (preço saía `2188.620000`).
    return styler.format({c: _fmt_dia for c in presentes}, subset=presentes)


def _layout(fig: go.Figure, altura: int, hovermode: str = "x unified", **extra) -> go.Figure:
    """Cromo comum: legenda horizontal acima do gráfico, hover unificado no
    eixo x, margens enxutas. Grade e fontes vêm do tema do Streamlit."""
    fig.update_layout(
        height=altura,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0, title_text=""),
        hovermode=hovermode,
        **extra,
    )
    return fig


def _marcar_dias(fig: go.Figure, marcas: dict[str, pd.Timestamp]) -> None:
    """Linha vertical fina nos dias escolhidos na aba (A/B ou o dia único),
    para o gráfico de série mostrar ONDE está a foto de cima."""
    for rotulo, dia in marcas.items():
        fig.add_vline(x=dia, line_width=1, line_color=Config.Colors.OUTRAS)
        # Rótulo DENTRO da área do gráfico, colado à linha: acima dela
        # disputaria espaço com a legenda horizontal.
        fig.add_annotation(x=dia, y=0.98, yref="paper", text=rotulo, showarrow=False,
                           yanchor="top", xanchor="left", xshift=3, font=dict(size=11))


# ── Detecção de Anomalias ────────────────────────────────────────────────────
def detectar_anomalias(share: pd.DataFrame, limpo: pd.DataFrame,
                       ganhos: pd.DataFrame, perdidos: pd.DataFrame) -> list[tuple[str, str]]:
    """Alertas automáticos como (nível, texto), nível em erro/aviso/info.

    Tudo compara o último dia OBSERVADO com o observado antes dele — dia sem
    coleta não entra como zero (ver aba Cobertura).
    """
    alertas: list[tuple[str, str]] = []

    # Queda de share, por plataforma: somar plataformas misturaria universos
    # de tamanhos diferentes e esconderia a queda de uma atrás da alta da outra.
    if not share.empty:
        for plataforma, bloco in share.sort_values("data").groupby("plataforma"):
            serie = bloco.set_index("data")["share_buybox_pct"].dropna()
            if len(serie) >= 2 and serie.iloc[-1] - serie.iloc[-2] <= -5:
                alertas.append(("aviso",
                    f"📉 Share em **{plataforma}** caiu "
                    f"{serie.iloc[-2] - serie.iloc[-1]:.1f} pp em {_fmt_dia(serie.index[-1])} "
                    f"({serie.iloc[-2]:.1f}% → {serie.iloc[-1]:.1f}%)."))

    n_ganhos, n_perdidos = len(ganhos), len(perdidos)
    if n_perdidos > n_ganhos * 2 and n_perdidos > 0:
        alertas.append(("erro",
            f"🔴 Perdas superam ganhos em mais de 2× ({n_perdidos} perdas × {n_ganhos} ganhos)."))

    # Concentração: um rival só levando boa parte das perdas é o sinal mais
    # acionável da tela — é com ELE que a régua de preço precisa ser revista.
    if n_perdidos >= 5:
        top = perdidos["seller_canonical"].value_counts()
        if top.iloc[0] / n_perdidos >= 0.4:
            alertas.append(("aviso",
                f"🥊 **{top.index[0]}** levou {top.iloc[0]} das suas {n_perdidos} perdas "
                f"({top.iloc[0] / n_perdidos:.0%}) — detalhe na aba Ganhos e perdas."))

    if not limpo.empty and limpo["posicao_mediana"].notna().any():
        pos = limpo.groupby("data")["posicao_mediana"].median().dropna()
        if len(pos) >= 2 and pos.iloc[-1] - pos.iloc[-2] > 5:
            alertas.append(("aviso",
                f"📉 Posição mediana piorou {pos.iloc[-1] - pos.iloc[-2]:.0f} posições "
                f"em {_fmt_dia(pos.index[-1])}."))

    return alertas


# ── Componentes de Visualização ──────────────────────────────────────────────
def render_kpi_cards(share: pd.DataFrame, limpo: pd.DataFrame, perdidos: pd.DataFrame) -> None:
    """Renderiza KPI cards com deltas temporais e cores semânticas."""
    if share.empty or limpo.empty:
        return

    c1, c2, c3, c4, c5 = st.columns(5)

    # Share de buy box com contexto
    ultimo_por_plat = share.groupby("plataforma")["data"].transform("max")
    hoje = share[share["data"] == ultimo_por_plat]
    detidos, universo = hoje["produtos_detidos"].sum(), hoje["produtos_universo"].sum()
    pct = 100.0 * detidos / universo if universo else 0.0

    # Calcular período anterior para delta
    periodo_anterior = share[share["data"] < hoje["data"].min()]
    delta_share = detidos_ant = None
    if not periodo_anterior.empty:
        ult_dia_anterior = periodo_anterior.groupby("plataforma")["data"].transform("max")
        anterior = periodo_anterior[periodo_anterior["data"] == ult_dia_anterior]
        detidos_ant = anterior["produtos_detidos"].sum()
        universo_ant = anterior["produtos_universo"].sum()
        pct_anterior = 100.0 * detidos_ant / universo_ant if universo_ant else 0.0
        delta_share = pct - pct_anterior

    ajuda_share = (
        "**O que é**: percentual de produtos com buy box detida.\n\n"
        f"**Como calculado**: {int(detidos)} de {int(universo)} produtos.\n\n"
        "**Período**: último dia observado por plataforma."
    )
    if delta_share is not None:
        ajuda_share += f"\n\n**Variação**: {delta_share:+.1f} pp vs o dia observado anterior."
    c1.metric(
        "Share de buy box",
        f"{pct:.1f}%",
        delta=f"{delta_share:+.1f}pp" if delta_share is not None else None,
        help=ajuda_share,
    )

    c2.metric(
        "Produtos com a BB",
        int(detidos),
        help=f"de {int(universo)} observados",
        delta=f"{int(detidos) - int(detidos_ant):+d}" if detidos_ant is not None else None,
    )

    c3.metric(
        "Ofertas monitoradas",
        f"{limpo['offer_key'].nunique():,}".replace(",", "."),
        help="Número único de ofertas (URL canônica) monitoradas"
    )

    ganhos_n = int(_sim(limpo["virou_no_turno"]).sum())
    perdidos_n = len(perdidos)
    saldo = ganhos_n - perdidos_n
    viradas = ganhos_n + perdidos_n
    # O delta antigo deste card era `ganhos − ganhos // 2` com um "%" colado
    # — metade dos ganhos, lida como percentual. Aqui vai um número que
    # significa algo: de todas as viradas que te envolveram, quantas você
    # venceu. Acima de 50% você mais toma do que perde.
    taxa = 100.0 * ganhos_n / viradas if viradas else None
    c4.metric(
        "Ganhos de buy box",
        ganhos_n,
        help="Produtos em que este seller tomou a BB de outro. O percentual é "
             "a **taxa de vitória nas viradas**: ganhos ÷ (ganhos + perdas).",
        delta=f"{taxa:.0f}% das viradas" if taxa is not None else None,
        delta_color="normal" if (taxa or 0) >= 50 else "inverse",
        delta_arrow="off",
    )

    c5.metric(
        "Perdidos",
        perdidos_n,
        help="Produtos em que outro seller tomou a BB deste.",
        delta=f"{saldo:+d} no saldo" if (ganhos_n or perdidos_n) else None,
        delta_color="normal" if saldo >= 0 else "inverse"
    )


def render_aba_share(share: pd.DataFrame) -> None:
    """Share de buy box por plataforma ao longo da janela."""
    if share.empty:
        st.info("Sem share no período — nenhum produto seu detinha a BB.")
        return

    pivo = share.pivot_table(index="data", columns="plataforma",
                             values="share_buybox_pct", aggfunc="mean")

    fig = go.Figure()
    for plataforma in pivo.columns:
        fig.add_trace(go.Scatter(
            x=pivo.index,
            y=pivo[plataforma],
            name=plataforma,
            mode="lines+markers",
            line=dict(color=_cor_plataforma(plataforma), width=2),
            marker=dict(size=6),
            hovertemplate=f"{plataforma}: %{{y:.1f}}%<extra></extra>",
        ))

    # Annotations para eventos importantes
    # Detectar quedas bruscas (>15pp em 1 dia)
    for plataforma in pivo.columns:
        serie = pivo[plataforma].dropna()
        if len(serie) > 1:
            quedas = serie.diff().dropna()
            for idx in quedas[quedas < -15].index:
                fig.add_annotation(
                    x=idx,
                    y=pivo.loc[idx, plataforma],
                    text="⚠️ Queda",
                    showarrow=True,
                    arrowhead=2,
                    arrowcolor=Config.Colors.PERDA[_tema()],
                    font=dict(size=10),
                )

    _layout(fig, Config.CHART_HEIGHT_SHARE, yaxis_title="Share de buy box (%)")
    fig.update_xaxes(tickformat="%d/%m")
    st.plotly_chart(fig, width="stretch")

    # Tabela detalhada com formatação condicional
    st.dataframe(
        _com_datas(
            share.sort_values(["data", "share_buybox_pct"], ascending=[False, False])[
                ["data", "plataforma", "produtos_detidos", "produtos_universo",
                 "share_buybox_pct"]
            ].rename(columns={
                "data": "Data", "plataforma": "Plataforma",
                "produtos_detidos": "Com a BB",
                "produtos_universo": "Universo observado",
                "share_buybox_pct": "Share %"
            }).style
            # vmin/vmax, não low/high: `low`/`high` são FRAÇÕES que esticam a
            # faixa de cor, e `high=100` comprimia tudo no tom mais claro.
            .background_gradient(subset=["Share %"], cmap="Greens", vmin=0, vmax=100)
            .format({"Share %": "{:.1f}%"})
            .highlight_max(subset=["Com a BB"], color=Config.Colors.PRIMARY + "33")),
        width="stretch",
        hide_index=True
    )

    st.caption(
        "O denominador é o universo de produtos **observados** na "
        "plataforma, não as suas linhas. Sobre as suas linhas o número "
        "daria sempre 100%: a vitrine só mostra quem detém a BB."
    )


# ── Aba Ganhos e perdas ──────────────────────────────────────────────────────
def _serie_eventos(ganhos: pd.DataFrame, perdidos: pd.DataFrame,
                   cobertura: pd.DataFrame, desde: date, freq: str) -> pd.DataFrame:
    """Ganhos e perdas por dia (ou semana) — dia SEM COLETA vira vazio, não 0.

    O gráfico antigo agrupava `data` como texto e reindexava por Timestamp:
    nenhum dia casava e a série inteira saía zerada. Com `data` tipada na
    borda (`_tipar`) o reindex casa — e sobra a outra metade do cuidado: um
    dia em que nenhuma plataforma de buy box foi coletada não teve "zero
    viradas", teve zero OLHADAS. Esse dia fica NaN (barra ausente).
    """
    datas = pd.concat([ganhos["data"], perdidos["data"], cobertura["data"]]).dropna()
    fim = max(datas.max(), pd.Timestamp(desde)) if not datas.empty else pd.Timestamp(desde)
    dias = pd.date_range(pd.Timestamp(desde), fim, freq="D")
    serie = pd.DataFrame({
        "ganhos": ganhos.groupby("data").size().reindex(dias, fill_value=0),
        "perdas": perdidos.groupby("data").size().reindex(dias, fill_value=0),
    }).astype(float)

    # Só as plataformas onde a disputa existe contam como "olhei": dia em
    # que só a loja própria foi coletada não diz nada sobre viradas.
    plataformas_bb = set(ganhos["plataforma"]) | set(perdidos["plataforma"])
    cob = cobertura[cobertura["plataforma"].isin(plataformas_bb)] if plataformas_bb else cobertura
    if not cob.empty:
        observados = pd.DatetimeIndex(cob.loc[_sim(cob["observado"]), "data"].unique())
        serie.loc[~serie.index.isin(observados), ["ganhos", "perdas"]] = np.nan

    if freq == "Semana":
        serie = serie.resample("W-MON", label="left", closed="left").sum(min_count=1)
    serie["saldo"] = serie["ganhos"] - serie["perdas"]
    serie["saldo_acum"] = serie["saldo"].fillna(0).cumsum()
    return serie


def _grafico_eventos(serie: pd.DataFrame, freq: str) -> go.Figure:
    """Viradas por período (ganho para cima, perda para baixo) e, num painel
    separado, o saldo acumulado.

    Painel separado, e não um segundo eixo y sobreposto: com dois eixos a
    posição relativa das duas escalas é arbitrária e o olho lê uma relação
    entre barra e linha que o dado não tem.
    """
    tema = _tema()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.68, 0.32], vertical_spacing=0.06)
    fig.add_trace(go.Bar(
        x=serie.index, y=serie["ganhos"], name="Ganhos",
        marker_color=Config.Colors.GANHO[tema],
        hovertemplate="Ganhos: %{y:.0f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=serie.index, y=-serie["perdas"], name="Perdas",
        marker_color=Config.Colors.PERDA[tema],
        customdata=serie["perdas"],
        hovertemplate="Perdas: %{customdata:.0f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=serie.index, y=serie["saldo_acum"], name="Saldo acumulado",
        mode="lines", line=dict(color=Config.Colors.TINTA[tema], width=2),
        hovertemplate="Saldo acumulado: %{y:+.0f}<extra></extra>",
    ), row=2, col=1)
    fig.add_hline(y=0, line_width=1, line_color=Config.Colors.OUTRAS, row=2, col=1)

    # Rótulo direto só no ponto que importa: onde o saldo termina.
    if not serie.empty:
        fim = serie["saldo_acum"].iloc[-1]
        fig.add_annotation(x=serie.index[-1], y=fim, text=f"<b>{fim:+.0f}</b>",
                           showarrow=False, xanchor="left", xshift=6, row=2, col=1)

    _layout(fig, 460, barmode="relative", bargap=0.25, barcornerradius=4)
    fig.update_yaxes(title_text="Viradas", row=1, col=1)
    fig.update_yaxes(title_text="Saldo", row=2, col=1)
    fig.update_xaxes(tickformat="%d/%m" if freq == "Dia" else "sem. %d/%m", row=2, col=1)
    return fig


def _rivais(ganhos: pd.DataFrame, perdidos: pd.DataFrame,
            precos: pd.DataFrame) -> pd.DataFrame:
    """Placar por concorrente: de quem você tomou, quem tomou de você.

    Ganho: `detentor_anterior` da MINHA linha (de quem tomei). Perda:
    `seller_canonical` da linha do OUTRO (quem levou). Os dois lados já estão
    carregados — nenhuma consulta nova.
    """
    placar = pd.concat([
        ganhos.groupby("detentor_anterior").size().rename("ganhos"),
        perdidos.groupby("seller_canonical").size().rename("perdas"),
    ], axis=1).fillna(0).astype(int)
    placar.index.name = "rival"
    placar["saldo"] = placar["ganhos"] - placar["perdas"]
    placar["total"] = placar["ganhos"] + placar["perdas"]
    if not precos.empty:
        placar["gap_mediano"] = precos.groupby("seller_canonical")["gap_pct"].median()
    else:
        placar["gap_mediano"] = np.nan
    return placar.sort_values(["total", "perdas"], ascending=False)


def _preco_na_perda(perdidos: pd.DataFrame, limpo: pd.DataFrame) -> pd.DataFrame:
    """Cada perda ao lado do SEU último preço no mesmo produto antes dela.

    "Quem me tirou a buy box, quando, e por quanto?" (§1.3 do documento do
    TPS). O preço do rival é o da linha dele no turno da virada; o seu é o da
    sua última observação como detentor do MESMO produto (mesma plataforma e
    `marketplace_product_id`) ANTES daquele turno — `merge_asof` para trás,
    sem casar o próprio turno. Os dois são preço de vitrine observado; nada
    aqui é custo ou margem.
    """
    # Toda perda continua na saída — sem par comparável ela só fica sem preço.
    base = perdidos.reset_index(drop=True).assign(meu_preco=np.nan, gap_pct=np.nan)
    if base.empty or limpo.empty:
        return base
    meus = _detidos(limpo).dropna(subset=["marketplace_product_id", "preco"])
    meus = (meus.assign(_t=_momento(meus))
            .dropna(subset=["_t"])[["plataforma", "marketplace_product_id", "_t", "preco"]]
            .rename(columns={"preco": "meu_preco"})
            .sort_values("_t"))
    perd = (base[["plataforma", "marketplace_product_id"]]
            .assign(_t=_momento(base), _i=np.arange(len(base)))
            .dropna(subset=["_t", "marketplace_product_id"])
            .sort_values("_t"))
    if meus.empty or perd.empty:
        return base
    casado = pd.merge_asof(perd, meus, on="_t",
                           by=["plataforma", "marketplace_product_id"],
                           direction="backward", allow_exact_matches=False)
    base.loc[casado["_i"].to_numpy(), "meu_preco"] = casado["meu_preco"].to_numpy()
    base["gap_pct"] = (base["preco"] / base["meu_preco"] - 1) * 100
    return base


def _grafico_rivais(placar: pd.DataFrame) -> go.Figure:
    """Borboleta: perdas para a esquerda, ganhos para a direita, um eixo só."""
    tema = _tema()
    topo = placar.head(10).iloc[::-1]   # Plotly desenha a 1ª categoria embaixo
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=topo.index, x=-topo["perdas"], orientation="h", name="Levou de você",
        marker_color=Config.Colors.PERDA[tema], customdata=topo["perdas"],
        hovertemplate="%{y} levou %{customdata} de você<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        y=topo.index, x=topo["ganhos"], orientation="h", name="Você tomou dele",
        marker_color=Config.Colors.GANHO[tema],
        hovertemplate="Você tomou %{x} de %{y}<extra></extra>",
    ))
    fig.add_vline(x=0, line_width=1, line_color=Config.Colors.OUTRAS)
    _layout(fig, max(260, 34 * len(topo) + 80), barmode="relative", bargap=0.3,
            barcornerradius=4, hovermode="closest")
    fig.update_xaxes(title_text="← perdas · ganhos →")
    return fig


def _grafico_quebra(ganhos: pd.DataFrame, perdidos: pd.DataFrame, dimensao: str) -> go.Figure:
    """Ganhos e perdas lado a lado por turno, plataforma ou marca."""
    tema = _tema()
    if dimensao == "Turno":
        chave_g, chave_p = ganhos["turno"], perdidos["turno"]
    elif dimensao == "Plataforma":
        chave_g, chave_p = ganhos["plataforma"], perdidos["plataforma"]
    else:
        chave_g, chave_p = _marca(ganhos), _marca(perdidos)
    tabela = pd.concat([ganhos.groupby(chave_g).size().rename("Ganhos"),
                        perdidos.groupby(chave_p).size().rename("Perdas")],
                       axis=1).fillna(0).astype(int)
    if dimensao == "Turno":
        tabela = tabela.loc[sorted(tabela.index, key=lambda t: TURNO_ORDEM.get(str(t).lower(), 9),
                                   reverse=True)]
    else:
        tabela = tabela.assign(_t=tabela.sum(axis=1)).sort_values("_t").drop(columns="_t").tail(12)
    viradas = tabela["Ganhos"] + tabela["Perdas"]
    taxa = (100 * tabela["Ganhos"] / viradas.where(viradas > 0)).round(0)
    fig = go.Figure()
    # Barra horizontal agrupada desenha o 1º traço EMBAIXO dentro do grupo:
    # Perdas entra primeiro para Ganhos ficar em cima, e a legenda inverte
    # para ler na mesma ordem das barras.
    for coluna, cor in (("Perdas", Config.Colors.PERDA[tema]), ("Ganhos", Config.Colors.GANHO[tema])):
        fig.add_trace(go.Bar(
            y=tabela.index, x=tabela[coluna], orientation="h", name=coluna,
            marker_color=cor, customdata=taxa,
            hovertemplate=f"{coluna}: %{{x}} · vitória nas viradas %{{customdata:.0f}}%<extra>%{{y}}</extra>",
        ))
    _layout(fig, max(220, 44 * len(tabela) + 80), barmode="group", bargap=0.3,
            bargroupgap=0.08, barcornerradius=4, hovermode="closest")
    fig.update_layout(legend_traceorder="reversed")
    return fig


def render_aba_ganhos_perdas(limpo: pd.DataFrame, perdidos: pd.DataFrame,
                             cobertura: pd.DataFrame, desde: date) -> None:
    ganhos = _ganhos(limpo) if not limpo.empty else limpo
    n_g, n_p = len(ganhos), len(perdidos)

    col_g, col_p, col_s, col_t = st.columns(4)
    col_g.metric("Ganhos na janela", n_g)
    col_p.metric("Perdidos na janela", n_p)
    col_s.metric("Saldo", f"{n_g - n_p:+d}")
    col_t.metric("Vitória nas viradas", f"{100 * n_g / (n_g + n_p):.0f}%" if (n_g + n_p) else "—",
                 help="Ganhos ÷ (ganhos + perdas): de todas as trocas de dono "
                      "que te envolveram, quantas você venceu.")
    st.caption(
        "Ganhei = tomei a BB de outro seller. Perdi = outro seller tomou "
        "a minha. Os dois só existem como EVENTO — a foto de um turno nunca "
        "mostra \"perdedor\", só quem está com a BB agora."
    )

    if ganhos.empty and perdidos.empty:
        st.info("Nenhum evento de ganho ou perda na janela.")
        return

    # ── Linha do tempo
    st.markdown("##### Viradas ao longo da janela")
    freq = st.segmented_control("Agrupar por", ["Dia", "Semana"], default="Dia",
                                required=True, key="gp_freq")
    serie = _serie_eventos(ganhos, perdidos, cobertura, desde, freq)
    st.plotly_chart(_grafico_eventos(serie, freq), width="stretch")
    sem_coleta = int(serie["ganhos"].isna().sum())
    st.caption(
        "Ganhos para cima, perdas para baixo; o painel de baixo acumula o saldo."
        + (f" {sem_coleta} {'dia' if freq == 'Dia' else 'semana'}(s) sem coleta "
           "aparecem vazios — não houve leitura, não \"zero virada\"." if sem_coleta else "")
    )
    with st.expander("Ver tabela da série"):
        st.dataframe(
            serie.rename_axis("Data").reset_index()
            .rename(columns={"ganhos": "Ganhos", "perdas": "Perdas",
                             "saldo": "Saldo", "saldo_acum": "Saldo acumulado"}),
            column_config={"Data": st.column_config.DateColumn(format="DD/MM/YYYY")},
            width="stretch", hide_index=True,
        )

    precos = _preco_na_perda(perdidos, limpo)
    placar = _rivais(ganhos, perdidos, precos)

    # ── Rivais
    st.markdown("##### Contra quem você disputa")
    st.plotly_chart(_grafico_rivais(placar), width="stretch")
    st.dataframe(
        placar.reset_index()[["rival", "ganhos", "perdas", "saldo", "gap_mediano"]]
        .rename(columns={"rival": "Rival", "ganhos": "Você tomou dele",
                         "perdas": "Ele tomou de você", "saldo": "Saldo",
                         "gap_mediano": "Preço dele vs o seu (mediana)"})
        .style
        .format({"Preço dele vs o seu (mediana)": _pct})
        .background_gradient(subset=["Saldo"], cmap="RdBu",
                             vmin=-max(1, placar["saldo"].abs().max()),
                             vmax=max(1, placar["saldo"].abs().max())),
        width="stretch", hide_index=True,
    )

    # ── Preço na perda
    st.markdown("##### Preço na perda")
    comparaveis = precos.dropna(subset=["gap_pct"])
    if comparaveis.empty:
        st.info("Nenhuma perda com o seu preço anterior observado no mesmo produto dentro da janela.")
    else:
        mais_barato = (comparaveis["gap_pct"] < 0).mean() * 100
        m1, m2, m3 = st.columns(3)
        m1.metric("Perdas com preço comparável", f"{len(comparaveis)} de {len(precos)}")
        m2.metric("Rival mais barato", f"{mais_barato:.0f}% das perdas",
                  help="Em quantas perdas o preço do rival no turno da virada era "
                       "menor que o seu último preço observado no mesmo produto.")
        m3.metric("Diferença mediana", _pct(comparaveis["gap_pct"].median()),
                  help="Preço do rival ÷ seu último preço − 1. Negativo = rival mais barato.")
        st.caption(
            "Leitura observada, não causa: além de preço, a BB também é decidida por "
            "frete, fulfillment e reputação. Os preços são os de vitrine coletados "
            "(a base de preço pode incluir desconto PIX conforme a plataforma)."
        )

    # ── Quebra
    st.markdown("##### Onde e quando as viradas acontecem")
    dimensao = st.segmented_control("Quebrar por", ["Turno", "Plataforma", "Marca"],
                                    default="Turno", required=True, key="gp_quebra")
    st.plotly_chart(_grafico_quebra(ganhos, perdidos, dimensao), width="stretch")
    st.caption("Passe o mouse para ver a taxa de vitória nas viradas de cada grupo. "
               "Turno com muita perda costuma ser a hora em que o rival reprecifica.")

    # ── Listas
    st.markdown("##### ✅ Produtos que você ganhou")
    if ganhos.empty:
        st.info("Nenhum ganho de buy box observado na janela.")
    else:
        st.dataframe(
            _com_datas(
                ganhos.assign(_ord=_momento(ganhos)).sort_values("_ord", ascending=False)[
                    ["data", "turno", "plataforma", "produto", "marca",
                     "detentor_anterior", "preco"]]
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "marca": "Marca",
                    "detentor_anterior": "Tomou de", "preco": "Preço"})
                .style
                .format({"Preço": _brl})),
            width="stretch",
            hide_index=True
        )

    st.markdown("##### ❌ Produtos que você perdeu")
    if perdidos.empty:
        st.info("Nenhuma perda de buy box observada na janela.")
    else:
        st.dataframe(
            _com_datas(
                # Comparação de preço antes do título do produto (a coluna
                # mais larga), para caber na tela sem rolar para o lado.
                precos.assign(_ord=_momento(precos)).sort_values("_ord", ascending=False)[
                    ["data", "turno", "plataforma", "seller_canonical", "preco",
                     "meu_preco", "gap_pct", "produto", "marca"]]
                .rename(columns={
                    "data": "Data", "turno": "Turno", "plataforma": "Plataforma",
                    "produto": "Produto", "marca": "Marca",
                    "seller_canonical": "Levou", "preco": "Preço dele",
                    "meu_preco": "Seu último preço", "gap_pct": "Diferença"})
                .style
                .format({"Preço dele": _brl, "Seu último preço": _brl, "Diferença": _pct})),
            width="stretch",
            hide_index=True
        )
    st.caption("Toda tabela exporta para CSV pelo ícone ⬇ que aparece ao passar o mouse sobre ela.")


# ── Aba Marcas e posição ─────────────────────────────────────────────────────
def _por_marca(detidos: pd.DataFrame) -> pd.Series:
    """Produtos distintos com a BB, por marca."""
    if detidos.empty:
        return pd.Series(dtype=int)
    return (detidos.assign(_produto=_chave_produto(detidos), _marca=_marca(detidos))
            .groupby("_marca")["_produto"].nunique())


def _grafico_barras_marca(por_marca: pd.Series) -> go.Figure:
    # Plotly respeita a ordem de chegada e desenha a PRIMEIRA categoria
    # embaixo — por isso a série entra em ordem CRESCENTE: a marca com mais
    # produtos (última) termina no topo, maior→menor de cima pra baixo.
    por_marca = por_marca.sort_values(ascending=True)
    fig = go.Figure(go.Bar(
        x=por_marca.values,
        y=por_marca.index,
        orientation="h",
        marker_color=Config.Colors.PRIMARY,
        hovertemplate="<b>%{y}</b><br>Produtos: %{x}<extra></extra>",
    ))
    fig.update_layout(
        height=max(Config.CHART_HEIGHT_MARCA, 28 * len(por_marca)),
        xaxis_title="Número de Produtos",
        yaxis_title="Marca",
        margin=dict(l=150, r=40, t=20, b=40),
        bargap=0.25,
        barcornerradius=4,
    )
    return fig


def _grafico_comparar_marcas(a: pd.Series, b: pd.Series, dia_a, dia_b) -> go.Figure:
    """Barras agrupadas A (cinza, contexto) × B (cor, foco) por marca."""
    tabela = pd.concat([a.rename("A"), b.rename("B")], axis=1).fillna(0)
    tabela = tabela.assign(_t=tabela.max(axis=1)).sort_values("_t").drop(columns="_t")
    fig = go.Figure()
    # B entra primeiro para A ficar EM CIMA no grupo (a barra horizontal
    # empilha o 1º traço embaixo): lê-se A → B de cima para baixo, e a
    # legenda invertida acompanha.
    fig.add_trace(go.Bar(y=tabela.index, x=tabela["B"], orientation="h",
                         name=f"B · {_fmt_dia(dia_b)}",
                         marker_color=Config.Colors.CATEGORICA[_tema()][0],
                         hovertemplate="B: %{x:.0f}<extra>%{y}</extra>"))
    fig.add_trace(go.Bar(y=tabela.index, x=tabela["A"], orientation="h",
                         name=f"A · {_fmt_dia(dia_a)}", marker_color=Config.Colors.OUTRAS,
                         hovertemplate="A: %{x:.0f}<extra>%{y}</extra>"))
    _layout(fig, max(Config.CHART_HEIGHT_MARCA, 40 * len(tabela) + 80), barmode="group",
            bargap=0.25, bargroupgap=0.08, barcornerradius=4, hovermode="y unified",
            legend_traceorder="reversed")
    fig.update_xaxes(title_text="Produtos com a BB")
    return fig


def _evolucao_marcas(detidos: pd.DataFrame, top_n: int = 6) -> pd.DataFrame:
    """Produtos com a BB por dia × marca; marcas fora do top-N viram "Outras".

    O top-N é pelo total da janela — a legenda fica estável enquanto a janela
    e o filtro de plataforma não mudam.
    """
    base = detidos.assign(_produto=_chave_produto(detidos), _marca=_marca(detidos))
    ranking = base.groupby("_marca")["_produto"].nunique().sort_values(ascending=False)
    principais = ranking.index[:top_n]
    base["_marca"] = base["_marca"].where(base["_marca"].isin(principais), "Outras")
    pivo = (base.groupby(["data", "_marca"])["_produto"].nunique()
            .unstack(fill_value=0))
    ordem = [m for m in principais if m in pivo.columns] + (["Outras"] if "Outras" in pivo.columns else [])
    return pivo[ordem]


def _grafico_evolucao_marcas(pivo: pd.DataFrame, modo: str) -> go.Figure:
    paleta = Config.Colors.CATEGORICA[_tema()]
    fig = go.Figure()
    for i, marca in enumerate(pivo.columns):
        cor = Config.Colors.OUTRAS if marca == "Outras" else paleta[i % len(paleta)]
        if modo == "% do portfólio":
            fig.add_trace(go.Scatter(
                x=pivo.index, y=pivo[marca], name=marca, mode="lines",
                stackgroup="um", groupnorm="percent",
                line=dict(color=cor, width=1), fillcolor=cor,
                hovertemplate=f"{marca}: %{{y:.1f}}%<extra></extra>",
            ))
        else:
            fig.add_trace(go.Scatter(
                x=pivo.index, y=pivo[marca], name=marca, mode="lines+markers",
                line=dict(color=cor, width=2), marker=dict(size=5),
                hovertemplate=f"{marca}: %{{y:.0f}}<extra></extra>",
            ))
    _layout(fig, 360, yaxis_title="% dos seus produtos com BB" if modo == "% do portfólio"
            else "Produtos com a BB")
    fig.update_xaxes(tickformat="%d/%m")
    if modo == "% do portfólio":
        fig.update_yaxes(range=[0, 100], ticksuffix="%")
    return fig


def _mudancas_portfolio(detidos: pd.DataFrame, dia_a, dia_b) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Produtos que entraram (com BB em B e não em A) e que saíram."""
    base = detidos.assign(_produto=_chave_produto(detidos), _marca=_marca(detidos))
    cols = ["plataforma", "_produto"]
    em_a = base[base["data"] == dia_a].drop_duplicates(cols)
    em_b = base[base["data"] == dia_b].drop_duplicates(cols)
    chave_a = set(map(tuple, em_a[cols].to_numpy()))
    chave_b = set(map(tuple, em_b[cols].to_numpy()))
    entrou = em_b[[tuple(k) not in chave_a for k in em_b[cols].to_numpy()]]
    saiu = em_a[[tuple(k) not in chave_b for k in em_a[cols].to_numpy()]]
    mostrar = {"_marca": "Marca", "produto": "Produto", "plataforma": "Plataforma", "preco": "Preço"}
    return (entrou[list(mostrar)].rename(columns=mostrar).sort_values(["Marca", "Produto"]),
            saiu[list(mostrar)].rename(columns=mostrar).sort_values(["Marca", "Produto"]))


def render_aba_marcas(limpo: pd.DataFrame) -> None:
    detidos = _detidos(limpo) if not limpo.empty else limpo
    st.markdown("##### Portfólio — produtos detidos por marca")
    marcas_dias: dict[str, pd.Timestamp] = {}

    if detidos.empty:
        st.info("Sem produto com buy box detida na janela.")
    else:
        dias = [pd.Timestamp(d) for d in sorted(detidos["data"].dropna().unique())]
        modo = st.segmented_control(
            "Período", ["Janela inteira", "Um dia", "Comparar dois dias"],
            default="Janela inteira", required=True, key="marcas_modo",
            help="Janela inteira soma todo produto que teve a sua BB em algum "
                 "turno da janela. Um dia mostra a foto daquele dia (qualquer "
                 "turno). Comparar mostra o que mudou entre duas datas.")

        if modo == "Um dia":
            # Sem `key` de propósito: as opções mudam com a janela, e o id do
            # widget muda junto — valor antigo fora da lista não sobrevive.
            dia = st.select_slider("Dia", options=dias, value=dias[-1], format_func=_fmt_dia)
            marcas_dias = {_fmt_dia(dia): dia}
            por_marca = _por_marca(detidos[detidos["data"] == dia])
            st.plotly_chart(_grafico_barras_marca(por_marca), width="stretch")
            st.caption(f"{int(por_marca.sum())} produtos com a sua BB em {_fmt_dia(dia)}, "
                       "somando os turnos do dia.")

        elif modo == "Comparar dois dias":
            if len(dias) < 2:
                st.info("A janela tem um dia só com BB detida — aumente a janela para comparar.")
            else:
                dia_a, dia_b = st.select_slider(
                    "Comparar A → B", options=dias,
                    value=(dias[max(0, len(dias) - 8)], dias[-1]), format_func=_fmt_dia)
                if dia_a == dia_b:
                    st.info("Escolha duas datas diferentes para comparar.")
                else:
                    marcas_dias = {"A": dia_a, "B": dia_b}
                    a = _por_marca(detidos[detidos["data"] == dia_a])
                    b = _por_marca(detidos[detidos["data"] == dia_b])
                    ta, tb = a.sum(), b.sum()
                    k1, k2, k3 = st.columns(3)
                    k1.metric(f"Produtos em {_fmt_dia(dia_a)}", int(ta))
                    k2.metric(f"Produtos em {_fmt_dia(dia_b)}", int(tb), delta=f"{int(tb - ta):+d}")
                    k3.metric("Marcas com BB", f"{(b > 0).sum()}",
                              delta=f"{int((b > 0).sum() - (a > 0).sum()):+d}")
                    st.plotly_chart(_grafico_comparar_marcas(a, b, dia_a, dia_b), width="stretch")

                    comp = pd.concat([a.rename("A"), b.rename("B")], axis=1).fillna(0).astype(int)
                    comp["Δ"] = comp["B"] - comp["A"]
                    comp["% portfólio A"] = 100 * comp["A"] / ta if ta else 0.0
                    comp["% portfólio B"] = 100 * comp["B"] / tb if tb else 0.0
                    comp["Δ pp"] = comp["% portfólio B"] - comp["% portfólio A"]
                    comp = comp.reindex(comp["Δ"].abs().sort_values(ascending=False).index)
                    lim = max(1, int(comp["Δ"].abs().max()))
                    st.dataframe(
                        comp.rename_axis("Marca").reset_index()
                        .rename(columns={"A": _fmt_dia(dia_a), "B": _fmt_dia(dia_b)})
                        .style
                        .format({"% portfólio A": "{:.1f}%", "% portfólio B": "{:.1f}%",
                                 "Δ pp": "{:+.1f}", "Δ": "{:+d}"})
                        .background_gradient(subset=["Δ"], cmap="RdBu", vmin=-lim, vmax=lim),
                        width="stretch", hide_index=True,
                    )
                    entrou, saiu = _mudancas_portfolio(detidos, dia_a, dia_b)
                    with st.expander(f"O que mudou: {len(entrou)} produtos entraram, "
                                     f"{len(saiu)} saíram do seu portfólio com BB"):
                        c_in, c_out = st.columns(2)
                        c_in.markdown("**Entraram** (BB em B, não em A)")
                        c_in.dataframe(entrou.style.format({"Preço": _brl}),
                                       width="stretch", hide_index=True)
                        c_out.markdown("**Saíram** (BB em A, não em B)")
                        c_out.dataframe(saiu.style.format({"Preço": _brl}),
                                        width="stretch", hide_index=True)
                    st.caption("Produto que \"saiu\" pode ter sido perdido para um rival "
                               "**ou** não ter sido coletado no dia B — confira a aba Cobertura.")

        else:
            por_marca = _por_marca(detidos)
            st.plotly_chart(_grafico_barras_marca(por_marca), width="stretch")

        # Evolução — responde "o mix de marcas está mudando?" sem precisar
        # comparar fotos uma a uma.
        st.markdown("##### Evolução do portfólio por marca")
        modo_evo = st.segmented_control("Medida", ["Produtos", "% do portfólio"],
                                        default="Produtos", required=True, key="marcas_evo")
        pivo = _evolucao_marcas(detidos)
        fig = _grafico_evolucao_marcas(pivo, modo_evo)
        _marcar_dias(fig, marcas_dias)
        st.plotly_chart(fig, width="stretch")
        with st.expander("Ver tabela da evolução"):
            st.dataframe(pivo.rename_axis("Data").reset_index(),
                         column_config={"Data": st.column_config.DateColumn(format="DD/MM/YYYY")},
                         width="stretch", hide_index=True)

    # Fora do `else` de propósito: com o filtro só em plataformas sem BB
    # exposta, o portfólio fica vazio e é ESTE aviso que explica o porquê.
    sem_buybox_exposta = (int(limpo.loc[limpo["detentor_buybox"].isna(), "offer_key"].nunique())
                          if not limpo.empty else 0)
    if sem_buybox_exposta:
        st.caption(
            f"⚠️ {sem_buybox_exposta} ofertas ficaram fora do portfólio por marca: "
            "estão em plataformas que não expõem vencedor de buy box na "
            "vitrine (ex.: Casas Bahia). A Amazon passou a expor via PDP "
            "(coletor Amazon-only no GitHub Actions, Set/2026), então já "
            "entra aqui."
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

        fig = go.Figure()
        for plataforma in pos.columns:
            fig.add_trace(go.Scatter(
                x=pos.index,
                y=pos[plataforma],
                name=plataforma,
                mode="lines+markers",
                line=dict(color=_cor_plataforma(plataforma), width=2),
                marker=dict(size=5),
                hovertemplate=f"{plataforma}: %{{y:.0f}}<extra></extra>",
            ))

        # Linha de referência Top 3
        fig.add_hline(
            y=3,
            line_dash="dash",
            annotation_text="Top 3 (Referência)",
            annotation_position="top right",
            line_color=Config.Colors.OUTRAS,
            line_width=1,
        )
        _marcar_dias(fig, marcas_dias)
        _layout(fig, Config.CHART_HEIGHT_POSICAO, yaxis_title="Posição mediana")
        fig.update_yaxes(autorange="reversed")  # Menor é melhor
        fig.update_xaxes(tickformat="%d/%m")
        st.plotly_chart(fig, width="stretch")

        st.caption(
            "Quanto menor, melhor — é a posição mediana entre as keywords em "
            "que a oferta apareceu no turno. Não soma entre plataformas nem "
            "vira ranking absoluto: é relativa a cada busca."
        )

    if not limpo.empty and limpo["tipo_seller"].notna().any():
        st.markdown("##### Como você aparece na vitrine")
        # Ofertas DISTINTAS: contar linhas somaria a mesma oferta uma vez por
        # turno observado.
        tipos = limpo.dropna(subset=["tipo_seller"]).groupby("tipo_seller")["offer_key"] \
                     .nunique().sort_values(ascending=False)
        st.dataframe(
            tipos.rename_axis("Tipo de seller").reset_index(name="Ofertas")
            .style
            .background_gradient(subset=["Ofertas"], cmap="Blues"),
            width="stretch",
            hide_index=True
        )


# ── Aba Ranking ──────────────────────────────────────────────────────────────
def render_aba_ranking(mercado: pd.DataFrame, share: pd.DataFrame, seller: str) -> None:
    plataformas_do_seller = sorted(share["plataforma"].unique()) if not share.empty else []
    if not plataformas_do_seller or mercado.empty:
        st.info("Sem ranking disponível — este seller não detém buy box em nenhuma plataforma na janela.")
        return

    mercado = mercado[mercado["plataforma"].isin(plataformas_do_seller)].copy()
    grupo = mercado.groupby(["data", "plataforma"])["share_buybox_pct"]
    mercado["posicao"] = grupo.rank(ascending=False, method="min")
    mercado["lider"] = grupo.transform("max")
    mercado["sellers"] = grupo.transform("size")

    # Série: a mesma régua da fotografia abaixo, aplicada a cada dia.
    meu = mercado[mercado["seller_canonical"] == seller].copy()
    if not meu.empty:
        st.markdown("##### Sua posição no ranking, dia a dia")
        pivo = meu.pivot_table(index="data", columns="plataforma", values="posicao", aggfunc="min")
        fig = go.Figure()
        for plataforma in pivo.columns:
            fig.add_trace(go.Scatter(
                x=pivo.index, y=pivo[plataforma], name=plataforma, mode="lines+markers",
                line=dict(color=_cor_plataforma(plataforma), width=2), marker=dict(size=5),
                hovertemplate=f"{plataforma}: #%{{y:.0f}}<extra></extra>",
            ))
        _layout(fig, 300, yaxis_title="Posição (#)")
        # Posição é inteira: marca de 1 em 1 enquanto couber; em ranking longo
        # o Plotly escolhe o passo (sempre inteiro, pelo `tickformat`).
        fig.update_yaxes(autorange="reversed", tickformat="d",
                         dtick=1 if pivo.max().max() <= 12 else None)
        fig.update_xaxes(tickformat="%d/%m")
        st.plotly_chart(fig, width="stretch")
        with st.expander("Ver tabela — posição e distância para o líder"):
            st.dataframe(
                meu.assign(gap=meu["share_buybox_pct"] - meu["lider"])
                .sort_values(["data", "plataforma"], ascending=[False, True])
                [["data", "plataforma", "posicao", "sellers", "share_buybox_pct", "lider", "gap"]]
                .rename(columns={"data": "Data", "plataforma": "Plataforma", "posicao": "#",
                                 "sellers": "Sellers", "share_buybox_pct": "Seu share %",
                                 "lider": "Share do líder %", "gap": "Distância (pp)"}),
                column_config={
                    "Data": st.column_config.DateColumn(format="DD/MM/YYYY"),
                    "#": st.column_config.NumberColumn(format="%d"),
                    "Seu share %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Share do líder %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Distância (pp)": st.column_config.NumberColumn(format="%+.1f"),
                },
                width="stretch", hide_index=True,
            )
        st.caption("Dia sem ponto = você não deteve nenhum produto naquela plataforma "
                   "(a view não tem linha de seller ausente) ou a coleta não rodou.")

    # Cada plataforma no seu próprio último dia observado (ver KPI): a
    # Amazon materializa com atraso e o max() global de `data` a apagava
    # deste ranking. Assim ela aparece mesmo um dia defasada.
    st.markdown("##### Fotografia do último dia observado")
    ultimo_por_plat = mercado.groupby("plataforma")["data"].transform("max")
    foto = mercado[mercado["data"] == ultimo_por_plat].copy()
    foto["posicao"] = foto["posicao"].astype(int)
    for plataforma in plataformas_do_seller:
        bloco = foto[foto["plataforma"] == plataforma].sort_values("posicao")
        total = len(bloco)
        linha_seller = bloco[bloco["seller_canonical"] == seller]
        if linha_seller.empty:
            continue
        minha_posicao = int(linha_seller["posicao"].iloc[0])
        dia_plat = bloco["data"].iloc[0]
        st.markdown(
            f"**{plataforma}** — você é **#{minha_posicao} de {total}** "
            f"· observado em {_fmt_dia(dia_plat)}"
        )
        topo = bloco.head(Config.TOP_RANKING_DISPLAY).copy()
        if minha_posicao > Config.TOP_RANKING_DISPLAY:
            topo = pd.concat([topo, linha_seller])
        topo["Você"] = topo["seller_canonical"].eq(seller).map({True: "✅", False: ""})
        st.dataframe(
            topo[["posicao", "seller_canonical", "Você", "produtos_detidos",
                  "produtos_universo", "share_buybox_pct"]]
            .rename(columns={
                "posicao": "#", "seller_canonical": "Seller",
                "produtos_detidos": "Com a BB",
                "produtos_universo": "Universo", "share_buybox_pct": "Share %"
            })
            .style
            .background_gradient(subset=["Share %"], cmap="Greens")
            .format({"Share %": "{:.1f}%"})
            .highlight_min(subset=["#"], color=Config.Colors.SUCCESS + "33"),
            width="stretch",
            hide_index=True
        )


# ── Aba Cobertura ────────────────────────────────────────────────────────────
def render_calendar_heatmap(cobertura: pd.DataFrame) -> None:
    """Renderiza calendar heatmap para cobertura."""
    if cobertura.empty:
        st.warning("Sem registro de cobertura no período.")
        return

    resumo = (cobertura.assign(observado=_sim(cobertura["observado"]))
              .groupby(["data", "plataforma"])
              .agg(turnos_observados=("observado", "sum"),
                   linhas=("linhas", "sum"))
              .reset_index())

    # Pivot para heatmap
    cobertura_pivot = resumo.pivot(
        index="plataforma", columns="data", values="turnos_observados"
    ).fillna(0)

    # Normalizar para porcentagem (0-3 turnos → 0-100%)
    cobertura_pct = (cobertura_pivot / 3 * 100).round(1)

    fig = go.Figure(data=go.Heatmap(
        z=cobertura_pct.values,
        x=[pd.Timestamp(x).strftime("%d/%m") for x in cobertura_pct.columns],
        y=cobertura_pct.index,
        colorscale=[
            [0, Config.Colors.DANGER],
            [0.33, Config.Colors.WARNING],
            [0.66, "#FFA500"],
            [1, Config.Colors.PRIMARY]
        ],
        showscale=True,
        colorbar=dict(title="Cobertura (%)"),
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Data: %{x}<br>"
            "Cobertura: %{z:.1f}%<br>"
            "<extra></extra>"
        ),
    ))

    fig.update_layout(
        title="Cobertura de Coleta por Plataforma e Data (Heatmap)",
        xaxis_title="Data",
        yaxis_title="Plataforma",
        height=max(200, len(cobertura_pct.index) * 50),
        margin=dict(l=100, r=40, t=60, b=60),
    )

    st.plotly_chart(fig, width="stretch")

    # Tabela de faltantes
    faltantes = resumo[resumo["turnos_observados"] < 3]
    if not faltantes.empty:
        st.warning(f"{len(faltantes)} combinações data×plataforma com menos de 3 turnos.")
        st.dataframe(
            _com_datas(
                faltantes.rename(columns={"data": "Data", "plataforma": "Plataforma",
                                          "turnos_observados": "Turnos observados",
                                          "linhas": "Linhas"})
                .style
                .background_gradient(subset=["Turnos observados"], cmap="RdYlGn", vmin=0, vmax=3)
                .format({"Turnos observados": "{:.0f}", "Linhas": "{:,.0f}"})),
            width="stretch",
            hide_index=True
        )
    else:
        st.success("Os 3 turnos foram observados em todas as plataformas.")


def _porta() -> bool:
    """Senha simples da demo. Não é autenticação de tenant — é um cadeado."""
    senha = st.secrets.get("SENHA")
    if not senha:
        return True
    if st.session_state.get("liberado"):
        return True
    st.title("Track Position Seller")
    digitada = st.text_input("Senha", type="password", key="senha_input")
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
    try:
        mercado = carregar_mercado(desde)
    except Exception as e:
        logger.error(f"Erro ao carregar mercado: {e}")
        st.sidebar.error(f"Falha ao carregar o mercado: {e}")
        mercado = _tipar(_quadro([], _COLUNAS_SHARE))
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

    dias = st.sidebar.slider(
        "Janela (dias)",
        Config.WINDOW_MIN,
        Config.WINDOW_MAX,
        Config.WINDOW_DEFAULT,
        help="Selecione o período de análise (mín: 3, máx: 60 dias)"
    )
    desde = date.today() - timedelta(days=dias)

    seller, mercado = _escolher_seller(desde)
    if not seller:
        return

    st.title(f"Track Position Seller — {seller}")
    st.caption(
        "Buy box, posição e concorrência nos marketplaces. Coleta em 3 turnos "
        "(08h Abertura · 14h Tarde · 20h Fechamento)."
    )

    # Carregar dados
    try:
        fato, share, cobertura = carregar(seller, desde)
    except Exception as e:
        st.error(f"Não foi possível carregar os dados de **{seller}** agora ({e}). "
                 "Tente de novo em instantes.")
        return
    try:
        perdidos = carregar_perdidos(seller, desde)
    except Exception as e:
        logger.error(f"Erro ao carregar perdidos de {seller}: {e}")
        st.warning("As **perdas** de buy box não puderam ser carregadas agora — "
                   "perdidos, saldo e rivais abaixo estão incompletos.")
        perdidos = _tipar(_quadro([], _SELECT_PERDIDOS))

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
                    + (f" … e mais {len(nomes) - 15}." if len(nomes) > 15 else ".")
                )

    # ── Só marketplace entra em KPI. Loja própria o lojista joga sozinho, e
    #    identidade suspeita é chave colapsada: nem numerador, nem denominador.
    limpo = fato[(fato["superficie"] == "marketplace").to_numpy(dtype=bool)
                 & ~_sim(fato["identidade_suspeita"])]

    # ── Filtro de plataforma: UM filtro, na sidebar, que vale para todas as
    #    abas e para os KPIs — filtro dentro de cada gráfico deixaria cada aba
    #    contando uma história de um recorte diferente.
    plataformas = sorted(set(limpo["plataforma"].dropna()) | set(share["plataforma"].dropna())
                         | set(perdidos["plataforma"].dropna()))
    escolha = st.sidebar.multiselect(
        "Plataformas", plataformas, placeholder="Todas",
        help="Recorta KPIs, gráficos e tabelas de todas as abas. Vazio = todas.")
    if escolha:
        limpo = limpo[limpo["plataforma"].isin(escolha)]
        share = share[share["plataforma"].isin(escolha)]
        perdidos = perdidos[perdidos["plataforma"].isin(escolha)]
        cobertura = cobertura[cobertura["plataforma"].isin(escolha)]
        mercado = mercado[mercado["plataforma"].isin(escolha)]

    if not vazio:
        render_kpi_cards(share, limpo, perdidos)

        alertas = detectar_anomalias(share, limpo, _ganhos(limpo), perdidos)
        if alertas:
            with st.expander(f"⚠️ {len(alertas)} alerta(s) na janela", expanded=True):
                for nivel, texto in alertas:
                    {"erro": st.error, "aviso": st.warning}.get(nivel, st.info)(texto)

    aba1, aba2, aba3, aba4, aba5 = st.tabs([
        "📈 Share de buy box", "🏆 Ganhos e perdas", "🏷️ Marcas e posição",
        "🥊 Ranking na plataforma", "🩺 Cobertura",
    ])

    with aba1:
        render_aba_share(share)

    with aba2:
        render_aba_ganhos_perdas(limpo, perdidos, cobertura, desde)

    with aba3:
        render_aba_marcas(limpo)

    with aba4:
        render_aba_ranking(mercado, share, seller)

    with aba5:
        st.markdown(
            "**Coleta ausente não é mercado vazio.** Turno sem leitura aparece "
            "aqui como não observado, e as células dele ficam fora de todo "
            "número acima — nunca viram zero."
        )

        # Calendar heatmap
        render_calendar_heatmap(cobertura)

    suspeitas = int(_sim(fato["identidade_suspeita"]).sum()) if not vazio else 0
    if suspeitas:
        st.divider()
        st.caption(
            f"⚠️ {suspeitas} ofertas ficaram fora dos números por identidade "
            "ambígua (chave de oferta colapsada na origem). Estão excluídas de "
            "propósito: entrariam somando produtos diferentes como se fossem um."
        )


if __name__ == "__main__":
    main()
