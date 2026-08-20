"""Interface Web para o Gerador de Audiobooks Jurídicos

Versão Robusta & Ultra Leve:
- Sem dependência externa de 'num2words' (conversão numérica 100% nativa).
- Sem sobrecarga de capas/imagens (arquivos MP3 ~30% mais leves).
- I/O não-bloqueante para escrita de metadados ID3 (run_in_executor).
- Cache inteligente em disco por hash MD5.
- Validação estrita de arquivos em disco para evitar FileNotFoundError.
- Fatiamento semântico de normas jurídicas (sem quebra de incisos e parágrafos).
"""

import asyncio
from functools import partial
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import List, Optional, Tuple
import unicodedata
import zipfile

import streamlit as st

# =====================================================================
# 1. MAPEAMENTOS, DICIONÁRIOS E CONVERSÃO NUMÉRICA NATIVA
# =====================================================================

ORDINAIS = {
    1: "primeiro", 2: "segundo", 3: "terceiro", 4: "quarto", 5: "quinto",
    6: "sexto", 7: "sétimo", 8: "oitavo", 9: "nono",
}
MAPA_ORDINAIS_REVERSO = {v: f"{k}º" for k, v in ORDINAIS.items()}
MAPA_ORDINAIS_REVERSO["setimo"] = "7º"

DICIONARIO_LATIM = [
    (re.compile(r"\bvacatio legis\b", re.I), "vacácio légis"),
    (re.compile(r"\bhabeas corpus\b", re.I), "hábeas córpus"),
    (re.compile(r"\bhabeas data\b", re.I), "hábeas dáta"),
    (re.compile(r"\bmutatis mutandis\b", re.I), "mutátis mutândis"),
    (re.compile(r"\bde cujus\b", re.I), "de cújus"),
    (re.compile(r"\berga omnes\b", re.I), "érga ómnes"),
    (re.compile(r"\bfumus boni iuris\b", re.I), "fúmus bóni iúris"),
    (re.compile(r"\bpericulum in mora\b", re.I), "perículum in móra"),
    (re.compile(r"\binaudita altera parte\b", re.I), "inaudíta áltera párte"),
    (re.compile(r"\bpacta sunt servanda\b", re.I), "pácta sûnt servânda"),
    (re.compile(r"\bex nunc\b", re.I), "éx nunc"),
    (re.compile(r"\bex tunc\b", re.I), "éx tunc"),
    (re.compile(r"\biuris tantum\b", re.I), "iúris tántum"),
    (re.compile(r"\biure et de iure\b", re.I), "iúre ét de iúre"),
    (re.compile(r"\bnon bis in idem\b", re.I), "non bis in ídem"),
    (re.compile(r"\bpro bono\b", re.I), "pro bôno"),
    (re.compile(r"\bquantum debeatur\b", re.I), "quántum debeátur"),
    (re.compile(r"\bmens legis\b", re.I), "mêns légis"),
    (re.compile(r"\bprima facie\b", re.I), "príma fácie"),
    (re.compile(r"\bex officio\b", re.I), "éx offício"),
    (re.compile(r"\bipso facto\b", re.I), "ípso fácto"),
    (re.compile(r"\bstare decisis\b", re.I), "stáre decísis"),
    (re.compile(r"\bsub judice\b", re.I), "sub júdice"),
    (re.compile(r"\bcontra legem\b", re.I), "cóntra légem"),
    (re.compile(r"\bsecundum legem\b", re.I), "secúndum légem"),
    (re.compile(r"\bpraeter legem\b", re.I), "préter légem"),
    (re.compile(r"\bcaput\b", re.I), "cáput"),
]

REGEX_NOTAS = [
    re.compile(r"\((?:Redação dada|Incluído|Vide|Revogado|Produção de efeito|Regulamento|Promulgação|Vigência|Restabelecimento)[^)]*\)", re.I),
    re.compile(r"\[(?:Redação dada|Incluído|Vide|Revogado|Produção de efeito|Regulamento|Promulgação|Vigência)[^\]]*\]", re.I),
    re.compile(r"(\d+)\s*\((?:um|dois|três|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|quatorze|catorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|[a-zá-ú\s]+)\)", re.I),
]

SIGLAS_JURIDICAS = [
    (re.compile(r"\bCF/88\b", re.I), "Constituição Federal"),
    (re.compile(r"\bCC/02\b", re.I), "Código Civil"),
    (re.compile(r"\bCPC\b", re.I), "Código de Processo Civil"),
    (re.compile(r"\bCPP\b", re.I), "Código de Processo Penal"),
    (re.compile(r"\bCLT\b", re.I), "C-L-T"),
    (re.compile(r"\bSTF\b", re.I), "S-T-F"),
    (re.compile(r"\bSTJ\b", re.I), "S-T-J"),
    (re.compile(r"\bTST\b", re.I), "T-S-T"),
    (re.compile(r"\bTRT\b", re.I), "T-R-T"),
    (re.compile(r"\bOAB\b", re.I), "O-A-B"),
    (re.compile(r"\bCNJ\b", re.I), "C-N-J"),
    (re.compile(r"\binc\.\s*", re.I), "inciso "),
    (re.compile(r"\bal\.\s*", re.I), "alínea "),
    (re.compile(r"\bfls?\.\s*", re.I), "folhas "),
    (re.compile(r"\bn[º°\.]\s*", re.I), "número "),
]

REGEX_REAIS = re.compile(r"R\$\s*(\d+(?:\.\d{3})*(?:,\d{1,2})?)")
REGEX_PORCENTO = re.compile(r"(\d+(?:,\d+)?)\s*%")
REGEX_LEIS = re.compile(r"\b(Lei|Decreto|Portaria|Medida Provisória|Resolução)\s*(?:n[º°\.]?)?\s*(\d+(?:\.\d+)?)\s*/\s*(\d{4})", re.I)
REGEX_PARAGRAFO_UNICO = re.compile(r"§\s*único\.?|Parágrafo\s+único\.?", re.I)
REGEX_PARAGRAFOS_SIMBOLO = re.compile(r"§§\s*")
REGEX_ARTIGOS = re.compile(r"\bArt(?:igo|\.)?\s*(\d+)[º°]?(?:-([A-Za-z]))?\.?", re.I)
REGEX_PARAGRAFOS = re.compile(r"§\s*(\d+)[º°]?(?:-([A-Za-z]))?\.?")
REGEX_INCISOS = re.compile(r"^\s*([IVXLCDM]+)\s*[-–—]\s*", re.MULTILINE)
REGEX_ALINEAS = re.compile(r"^\s*([a-z])\)\s*", re.MULTILINE)
REGEX_DIVISAO_MAIOR = re.compile(r"^(?:TÍTULO|CAPÍTULO|SEÇÃO|SUBSEÇÃO|LIVRO)\b", re.I)
REGEX_DIVISAO_ART = re.compile(r"^Artigo\s+\w+", re.I)

VOZ_FALLBACK_PADRAO = "pt-BR-FranciscaNeural"
DIR_CACHE_GLOBAL = os.path.join(tempfile.gettempdir(), "audiobook_tts_cache")
os.makedirs(DIR_CACHE_GLOBAL, exist_ok=True)


def numero_para_extenso_ptbr(n: int) -> str:
    """Converte números inteiros para extenso em português nativamente."""
    if n == 0:
        return "zero"
    
    unidades = ["", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove"]
    de_10_a_19 = ["dez", "onze", "doze", "treze", "quatorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove"]
    dezenas = ["", "dez", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa"]
    centenas = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos", "setecentos", "oitocentos", "novecentos"]
    
    if n == 100:
        return "cem"
    
    partes = []
    
    # Milhões
    milhoes = n // 1_000_000
    resto = n % 1_000_000
    if milhoes > 0:
        if milhoes == 1:
            partes.append("um milhão")
        else:
            partes.append(f"{numero_para_extenso_ptbr(milhoes)} milhões")
    
    # Milhares
    milhares = resto // 1_000
    resto = resto % 1_000
    if milhares > 0:
        if milhares == 1:
            partes.append("mil")
        else:
            partes.append(f"{numero_para_extenso_ptbr(milhares)} mil")
            
    # Centenas, dezenas e unidades
    if resto > 0:
        c = resto // 100
        d = (resto % 100) // 10
        u = resto % 10
        
        texto_resto = []
        if c > 0:
            if resto == 100:
                texto_resto.append("cem")
            else:
                texto_resto.append(centenas[c])
        
        du = resto % 100
        if 10 <= du <= 19:
            texto_resto.append(de_10_a_19[du - 10])
        else:
            if d > 0:
                texto_resto.append(dezenas[d])
            if u > 0:
                texto_resto.append(unidades[u])
                
        if texto_resto:
            partes.append(" e ".join(texto_resto))
        
    return " e ".join(partes)


def reais_para_extenso_ptbr(valor: float) -> str:
    """Converte valores monetários para extenso em português nativamente."""
    inteiro = int(valor)
    centavos = int(round((valor - inteiro) * 100))
    
    partes = []
    if inteiro > 0:
        ext_int = numero_para_extenso_ptbr(inteiro)
        sufixo = "real" if inteiro == 1 else "reais"
        partes.append(f"{ext_int} {sufixo}")
    elif centavos == 0:
        return "zero reais"
        
    if centavos > 0:
        ext_cent = numero_para_extenso_ptbr(centavos)
        sufixo_cent = "centavo" if centavos == 1 else "centavos"
        partes.append(f"{ext_cent} {sufixo_cent}")
        
    return " e ".join(partes)

# =====================================================================
# 2. FUNÇÕES DE PROCESSAMENTO E NORMALIZAÇÃO
# =====================================================================

def formatar_taxa_velocidade(fator_velocidade: float) -> str:
    percentual = round((fator_velocidade - 1.0) * 100)
    sinal = "+" if percentual >= 0 else ""
    return f"{sinal}{percentual}%"

def romano_para_inteiro(romano: str) -> int:
    tabela = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, anterior = 0, 0
    for char in reversed(romano.upper()):
        valor = tabela.get(char, 0)
        total += -valor if valor < anterior else valor
        anterior = valor
    return total

def normalizar_unicode(texto: str) -> str:
    if not texto:
        return ""
    return unicodedata.normalize("NFC", texto).replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")

def converter_caixa_alta_estruturada(texto: str) -> str:
    linhas = texto.split("\n")
    novas_linhas = []
    for linha in linhas:
        l_strip = linha.strip()
        if len(l_strip) > 4 and l_strip.isupper():
            novas_linhas.append(l_strip.capitalize())
        else:
            novas_linhas.append(linha)
    return "\n".join(novas_linhas)

def limpar_texto_legislativo(texto: str) -> str:
    texto = normalizar_unicode(texto)
    for p in REGEX_NOTAS[:2]:
        texto = p.sub("", texto)
    texto = REGEX_NOTAS[2].sub(r"\1", texto)
    return texto

def extrair_texto_web(url: str) -> str:
    from bs4 import BeautifulSoup
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    sessao = requests.Session()
    estrategia = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    sessao.mount("http://", HTTPAdapter(max_retries=estrategia))
    sessao.mount("https://", HTTPAdapter(max_retries=estrategia))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    
    try:
        response = sessao.get(url, headers=headers, timeout=25)
        response.raise_for_status()
        if not response.content:
            raise ValueError("URL retornou conteúdo vazio")
    except requests.exceptions.HTTPError:
        if response.status_code == 403:
            raise ValueError("O site (ex: Jusbrasil) bloqueou o acesso automático (Erro 403). Use a aba '✍️ Colar Texto' ou use o link oficial do Planalto/Senado.")
        raise ValueError(f"Erro HTTP {response.status_code} ao acessar a URL.")
    except requests.exceptions.Timeout:
        raise TimeoutError(f"Timeout ao acessar {url}. O servidor de destino demorou a responder.")
    except requests.exceptions.ConnectionError:
        raise ConnectionError(f"Não foi possível conectar a {url}. Verifique sua conexão.")

    if response.encoding and response.encoding.lower() in ["iso-8859-1", "iso8859-1"]:
        response.encoding = "windows-1252"
    elif not response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    
    for tag in soup.find_all(["script", "style", "header", "footer", "nav", "aside", "noscript", "form", "button", "iframe", "svg"]):
        tag.decompose()
    for tag_revogada in soup.find_all(["strike", "del", "s"]):
        tag_revogada.decompose()
    for elemento in soup.find_all(class_=re.compile(r"(revogad|tachad|cancelad|strike|texto-revogado)", re.I)):
        elemento.decompose()

    conteudo = (
        soup.find("article")
        or soup.find("main")
        or soup.find(id=re.compile(r"(conteudo|content|texto-norma|documento)", re.I))
        or soup.find(class_=re.compile(r"(texto-norma|corpo-lei|conteudo-lei|artigos)", re.I))
        or soup.body
    )
    texto = conteudo.get_text(separator="\n") if conteudo else soup.get_text(separator="\n")
    if not texto.strip():
        raise ValueError("Nenhum texto aproveitável foi localizado.")
    return limpar_texto_legislativo(texto)

def extrair_texto_pdf(arquivo_pdf_bytes: io.BytesIO) -> str:
    from pypdf import PdfReader
    leitor = PdfReader(arquivo_pdf_bytes)
    paginas = [p.extract_text() for p in leitor.pages if p.extract_text()]
    return limpar_texto_legislativo("\n".join(paginas))

@st.cache_data(show_spinner=False, ttl=3600)
def extrair_texto_web_cached(url: str) -> str:
    return extrair_texto_web(url)

@st.cache_data(show_spinner=False)
def normalizar_texto_juridico(texto: str) -> str:
    texto = normalizar_unicode(texto)
    texto = converter_caixa_alta_estruturada(texto)

    for padrao, subst in DICIONARIO_LATIM:
        texto = padrao.sub(subst, texto)

    def _reais(m):
        try:
            v = float(m.group(1).replace(".", "").replace(",", "."))
            return reais_para_extenso_ptbr(v)
        except Exception:
            return m.group(0)

    texto = REGEX_REAIS.sub(_reais, texto)
    texto = REGEX_PORCENTO.sub(r"\1 por cento", texto)
    texto = REGEX_LEIS.sub(lambda m: f"{m.group(1)} número {m.group(2).replace('.', '')} de {m.group(3)}. ", texto)

    for padrao, subst in SIGLAS_JURIDICAS:
        texto = padrao.sub(subst, texto)

    texto = REGEX_PARAGRAFO_UNICO.sub("Parágrafo único. ", texto)
    texto = REGEX_PARAGRAFOS_SIMBOLO.sub("Parágrafos ", texto)
    texto = REGEX_ARTIGOS.sub(
        lambda m: f"Artigo {ORDINAIS[int(m.group(1))] if 1 <= int(m.group(1)) <= 9 else m.group(1)}{' ' + m.group(2) if m.group(2) else ''}. ",
        texto,
    )
    texto = REGEX_PARAGRAFOS.sub(
        lambda m: f"Parágrafo {ORDINAIS[int(m.group(1))] if 1 <= int(m.group(1)) <= 9 else m.group(1)}{' ' + m.group(2) if m.group(2) else ''}. ",
        texto,
    )

    def _sub_inciso(m):
        num = romano_para_inteiro(m.group(1).upper())
        extenso = numero_para_extenso_ptbr(num) if num > 0 else m.group(1)
        return f"Inciso {extenso}. "

    texto = REGEX_INCISOS.sub(_sub_inciso, texto)
    texto = REGEX_ALINEAS.sub(r"Alínea \1. ", texto)
    texto = re.sub(r"\.\s*\.", ".", texto)
    texto = re.sub(r"\n\s*\n+", "\n\n", texto)
    return re.sub(r"[ \t]+", " ", texto).strip()

def extrair_intervalo_artigos(texto_bloco: str) -> Tuple[str, str]:
    matches = re.findall(r"\bArtigo\s+([a-zá-ú0-9]+(?:\s+[A-Za-z]|-[A-Za-z])?)", texto_bloco, flags=re.IGNORECASE)
    if not matches:
        return ("Preâmbulo e Disposições Iniciais", "Inicio_e_Disposicoes")

    def formatar_artigo(art_str: str) -> str:
        partes = art_str.strip().split()
        num_ord = partes[0].lower()
        sufixo = f"-{partes[1].upper()}" if len(partes) > 1 else (f"-{art_str.split('-')[1].upper()}" if "-" in art_str else "")
        return f"{MAPA_ORDINAIS_REVERSO.get(num_ord, num_ord)}{sufixo}"

    arts = [formatar_artigo(m) for m in matches]
    if len(arts) == 1 or arts[0] == arts[-1]:
        rotulo, rotulo_arq = f"Art. {arts[0]}", f"Art_{arts[0]}"
    else:
        rotulo, rotulo_arq = f"Arts. {arts[0]} ao {arts[-1]}", f"Arts_{arts[0]}_ao_{arts[-1]}"

    rotulo_arq = rotulo_arq.replace(" ", "_").replace(".", "").replace("º", "o").replace("°", "o").replace("/", "-")
    return rotulo, rotulo_arq

@st.cache_data(show_spinner=False)
def dividir_texto_em_blocos_estruturados(texto: str, minutos_por_faixa: int = 5) -> List[str]:
    max_palavras = minutos_por_faixa * 140
    linhas = [l.strip() for l in texto.split("\n") if l.strip()]
    
    unidades, unidade_atual = [], []
    for linha in linhas:
        if REGEX_DIVISAO_MAIOR.match(linha) or REGEX_DIVISAO_ART.match(linha):
            if unidade_atual:
                unidades.append("\n".join(unidade_atual))
            unidade_atual = [linha]
        else:
            unidade_atual.append(linha)
    if unidade_atual:
        unidades.append("\n".join(unidade_atual))

    blocos, bloco_atual = [], []
    total_palavras = 0
    for unidade in unidades:
        palavras_u = len(unidade.split())
        if total_palavras + palavras_u > max_palavras:
            if bloco_atual:
                blocos.append("\n".join(bloco_atual))
            bloco_atual = [unidade]
            total_palavras = palavras_u
        else:
            bloco_atual.append(unidade)
            total_palavras += palavras_u
    if bloco_atual:
        blocos.append("\n".join(bloco_atual))
    return blocos

def _escrita_id3_leve(caminho_mp3: str, titulo: str, album: str, artista: str, faixa_num: int, total_faixas: int):
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCON, TIT2, TPE1, TRCK
    try:
        tags = ID3(caminho_mp3)
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text=titulo))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TPE1(encoding=3, text=artista))
    tags.add(TRCK(encoding=3, text=f"{faixa_num}/{total_faixas}"))
    tags.add(TCON(encoding=3, text="Audiobook / Direito"))
    tags.save(caminho_mp3, v2_version=3)

# =====================================================================
# 3. MOTOR TTS ASSÍNCRONO ULTRA RÁPIDO COM CACHE E THREADPOOL
# =====================================================================

def calcular_hash_faixa(texto: str, voz: str, taxa: str, pitch: str) -> str:
    conteudo = f"{texto}|{voz}|{taxa}|{pitch}".encode("utf-8")
    return hashlib.md5(conteudo).hexdigest()

async def sintetizar_com_retry_e_cache(
    texto: str,
    caminho_saida: str,
    voz: str,
    taxa: str,
    pitch: str,
    semaforo: asyncio.Semaphore,
    max_tentativas: int = 3,
) -> bool:
    import edge_tts
    
    hash_id = calcular_hash_faixa(texto, voz, taxa, pitch)
    caminho_cache = os.path.join(DIR_CACHE_GLOBAL, f"{hash_id}.mp3")

    if os.path.exists(caminho_cache) and os.path.getsize(caminho_cache) > 0:
        shutil.copyfile(caminho_cache, caminho_saida)
        return True

    voz_atual = voz
    for tentativa in range(1, max_tentativas + 1):
        try:
            async with semaforo:
                comunicador = edge_tts.Communicate(
                    text=texto, 
                    voice=voz_atual, 
                    rate=taxa, 
                    pitch=pitch,
                    receive_timeout=15
                )
                await comunicador.save(caminho_saida)
            
            if os.path.exists(caminho_saida) and os.path.getsize(caminho_saida) > 0:
                shutil.copyfile(caminho_saida, caminho_cache)
            return True
        except Exception:
            if tentativa == max_tentativas - 1 and voz != VOZ_FALLBACK_PADRAO:
                voz_atual = VOZ_FALLBACK_PADRAO
            if tentativa < max_tentativas:
                await asyncio.sleep(1.2 ** tentativa)
            else:
                return False

async def processar_faixa_individual(
    bloco: str,
    idx: int,
    total_partes: int,
    prefixo_limpo: str,
    nome_norma: str,
    pasta_saida: str,
    voz: str,
    taxa: str,
    pitch: str,
    semaforo: asyncio.Semaphore,
    progresso_tracker: dict,
) -> Optional[Tuple[int, str, str]]:
    loop = asyncio.get_running_loop()
    num_str = f"{idx:02d}"
    rotulo_artigos, rotulo_arq = extrair_intervalo_artigos(bloco)
    nome_arq = f"{num_str}_{prefixo_limpo}_{rotulo_arq}.mp3"
    caminho_mp3 = os.path.join(pasta_saida, nome_arq)
    intro_audio = f"{nome_norma}. Faixa {idx} de {total_partes}. {rotulo_artigos}.\n\n{bloco}"

    sucesso = await sintetizar_com_retry_e_cache(
        texto=intro_audio,
        caminho_saida=caminho_mp3,
        voz=voz,
        taxa=taxa,
        pitch=pitch,
        semaforo=semaforo,
    )

    if not sucesso:
        progresso_tracker["erros"].append(f"Faixa {idx:02d} ({rotulo_artigos}) falhou após retentativas.")
        return None

    await loop.run_in_executor(
        None,
        partial(
            _escrita_id3_leve,
            caminho_mp3,
            f"Faixa {num_str} ({rotulo_artigos}) - {nome_norma}",
            nome_norma,
            "Legislação em Áudio",
            idx,
            total_partes,
        ),
    )

    progresso_tracker["concluidos"] += 1
    progresso_tracker["barra"].progress(progresso_tracker["concluidos"] / total_partes)
    progresso_tracker["status"].text(f"🎙 Convertidas {progresso_tracker['concluidos']}/{total_partes} faixas...")

    return (idx, caminho_mp3, rotulo_artigos)

async def sintetizar_faixas_async(
    blocos: List[str],
    nome_norma: str,
    pasta_saida: str,
    voz: str,
    taxa: str,
    pitch: str,
    barra_progresso,
    status_texto,
    max_concorrencia: int = 10,
) -> List[Tuple[str, str]]:
    prefixo_limpo = re.sub(r"[^\w\-]", "_", nome_norma)
    total_partes = len(blocos)
    semaforo = asyncio.Semaphore(max_concorrencia)

    progresso_tracker = {
        "concluidos": 0,
        "erros": [],
        "barra": barra_progresso,
        "status": status_texto,
    }

    tarefas = [
        processar_faixa_individual(
            bloco=bloco,
            idx=idx,
            total_partes=total_partes,
            prefixo_limpo=prefixo_limpo,
            nome_norma=nome_norma,
            pasta_saida=pasta_saida,
            voz=voz,
            taxa=taxa,
            pitch=pitch,
            semaforo=semaforo,
            progresso_tracker=progresso_tracker,
        )
        for idx, bloco in enumerate(blocos, start=1)
    ]

    resultados_brutos = await asyncio.gather(*tarefas, return_exceptions=True)
    resultados_validos = [r for r in resultados_brutos if isinstance(r, tuple) and r is not None]
    resultados_ordenados = sorted(resultados_validos, key=lambda x: x[0])
    lista_mp3 = [(caminho, rotulo) for _, caminho, rotulo in resultados_ordenados]

    if progresso_tracker["erros"]:
        for falha in progresso_tracker["erros"]:
            st.warning(f"⚠️ {falha}")

    caminho_m3u = os.path.join(pasta_saida, f"{prefixo_limpo}.m3u")
    with open(caminho_m3u, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for caminho, _ in lista_mp3:
            f.write(f"{os.path.basename(caminho)}\n")

    return lista_mp3

# =====================================================================
# 4. INTERFACE E GERENCIAMENTO DE ESTADO STREAMLIT
# =====================================================================

st.set_page_config(
    page_title="Gerador de Audiobooks Jurídicos",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "resultado_gerado" not in st.session_state:
    st.session_state.resultado_gerado = None
if "caminho_zip_disco" not in st.session_state:
    st.session_state.caminho_zip_disco = None
if "pasta_temporaria_ativa" not in st.session_state:
    st.session_state.pasta_temporaria_ativa = None

st.title("⚖️ Gerador de Audiobooks Jurídicos")
st.markdown("Transforme normas e leis em áudio de alta velocidade dividido por artigos.")

with st.sidebar:
    st.header("⚙️ Configurações de Áudio")

    vozes_opcoes = {
        "Antônio (Masculino - Sóbrio e Grave)": "pt-BR-AntonioNeural",
        "Francisca (Feminino - Natural)": "pt-BR-FranciscaNeural",
        "Thalita (Feminino - Jovem)": "pt-BR-ThalitaNeural",
        "Nicolau (Masculino - Claro)": "pt-BR-NicolauNeural",
    }
    voz_selecionada = st.selectbox("Voz Neural:", list(vozes_opcoes.keys()))
    voz_code = vozes_opcoes[voz_selecionada]

    velocidade_fator = st.slider(
        "⚡ Velocidade da Leitura:",
        min_value=0.8,
        max_value=2.0,
        value=1.15,
        step=0.05,
        format="%.2fx",
        help="Ajuste a cadência da fala."
    )
    taxa_tts = formatar_taxa_velocidade(velocidade_fator)

    pitch_fator = st.select_slider(
        "🎚 Tom de Voz (Pitch):",
        options=["-15Hz", "-10Hz", "-5Hz", "+0Hz", "+5Hz"],
        value="-10Hz",
    )

    minutos_faixa = st.number_input(
        "⏱ Duração Máx. por Faixa (Minutos):", 
        min_value=2, 
        max_value=20, 
        value=5,
        help="Faixas de 4 a 6 min aumentam a velocidade do paralelismo."
    )

    st.markdown("---")
    max_concorrencia = st.select_slider(
        "🚀 Concorrência Paralela (Threads):",
        options=[4, 6, 8, 10, 12, 16],
        value=10,
        help="Quantidade de downloads simultâneos."
    )

st.subheader("1. Escolha a Fonte da Legislação")
nome_lei_input = st.text_input("Título / Nome do Audiobook:", value="Lei Geral de Proteção de Dados - LGPD")
aba_url, aba_pdf, aba_texto = st.tabs(["🌐 Link da Web (Planalto / Senado)", "📄 Arquivo PDF", "✍️ Colar Texto"])

with aba_url:
    url_input = st.text_input("URL da Lei:", value="https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709.htm")

with aba_pdf:
    pdf_upload = st.file_uploader("Selecione um arquivo PDF de legislação:", type=["pdf"])

with aba_texto:
    texto_direto = st.text_area("Cole o texto da lei aqui:", height=160)

st.markdown("---")

if st.button("🚀 Gerar Audiobook Completo", type="primary", use_container_width=True):
    # Reseta estados anteriores
    if st.session_state.pasta_temporaria_ativa and os.path.exists(st.session_state.pasta_temporaria_ativa):
        shutil.rmtree(st.session_state.pasta_temporaria_ativa, ignore_errors=True)
    st.session_state.resultado_gerado = None
    st.session_state.caminho_zip_disco = None

    pasta_temporaria = tempfile.mkdtemp()
    st.session_state.pasta_temporaria_ativa = pasta_temporaria

    with st.status("Processando legislação...", expanded=True) as status:
        try:
            texto_bruto = None
            if pdf_upload is not None:
                st.write("📄 Lendo PDF...")
                texto_bruto = extrair_texto_pdf(io.BytesIO(pdf_upload.read()))
            elif texto_direto.strip():
                st.write("✍️ Processando texto colado...")
                texto_bruto = limpar_texto_legislativo(texto_direto)
            elif url_input.strip():
                st.write(f"🌐 Buscando texto em `{url_input}`...")
                texto_bruto = extrair_texto_web_cached(url_input)
            else:
                st.error("❌ Por favor, forneça uma URL, envie um PDF ou cole o texto.")
                st.stop()

            if not texto_bruto or not texto_bruto.strip():
                st.error("❌ Nenhum texto foi encontrado na fonte.")
                st.stop()

            st.write("🔤 Aplicando pronúncia fonética e normalização...")
            texto_limpo = normalizar_texto_juridico(texto_bruto)

            st.write(f"✂️ Fatiando artigos em blocos de ~{minutos_faixa} minutos...")
            blocos = dividir_texto_em_blocos_estruturados(texto_limpo, minutos_por_faixa=minutos_faixa)
            st.write(f"📊 Total de faixas: **{len(blocos)}**")

            st.write("🎙 Sintetizando áudios em paralelo (com cache em disco)...")
            progresso = st.progress(0)
            status_txt = st.empty()

            lista_faixas = asyncio.run(
                sintetizar_faixas_async(
                    blocos=blocos,
                    nome_norma=nome_lei_input,
                    pasta_saida=pasta_temporaria,
                    voz=voz_code,
                    taxa=taxa_tts,
                    pitch=pitch_fator,
                    barra_progresso=progresso,
                    status_texto=status_txt,
                    max_concorrencia=max_concorrencia,
                )
            )

            if not lista_faixas:
                st.error("Nenhuma faixa pôde ser sintetizada.")
                st.stop()

            st.write("⚡ Gerando pacote ZIP de download...")
            prefixo_arq = re.sub(r"[^\w\-]", "_", nome_lei_input)
            caminho_zip = os.path.join(pasta_temporaria, f"{prefixo_arq}_audiobook.zip")

            with zipfile.ZipFile(caminho_zip, "w", zipfile.ZIP_STORED) as zip_file:
                for root, _, files in os.walk(pasta_temporaria):
                    for file in files:
                        if not file.endswith(".zip"):
                            zip_file.write(os.path.join(root, file), arcname=file)

            st.session_state.resultado_gerado = {
                "nome_lei": nome_lei_input,
                "faixas": lista_faixas,
                "velocidade": velocidade_fator,
                "texto_limpo": texto_limpo,
            }
            st.session_state.caminho_zip_disco = caminho_zip

            status.update(label="✅ Audiobook gerado com sucesso!", state="complete")

        except Exception as e:
            st.error(f"❌ Ocorreu um erro: {str(e)}")
            st.stop()

# =====================================================================
# 5. TOCADOR, DOWNLOADS E TEXTO NORMALIZADO (COM VALIDAÇÃO DE ARQUIVO)
# =====================================================================

if st.session_state.resultado_gerado and st.session_state.caminho_zip_disco:
    caminho_zip = st.session_state.caminho_zip_disco

    # Verifica se o arquivo físico realmente existe no disco
    if os.path.exists(caminho_zip):
        dados = st.session_state.resultado_gerado
        tamanho_mb = os.path.getsize(caminho_zip) / (1024 * 1024)

        st.success(f"🎉 **{len(dados['faixas'])} faixas** geradas com sucesso a **{dados['velocidade']}x**!")

        st.markdown("### 🎧 Reproduzir e Baixar Faixas Individuais")
        nomes_faixas = [os.path.basename(f[0]) for f in dados["faixas"]]
        faixa_selecionada = st.selectbox("Selecione a faixa para reproduzir:", nomes_faixas)

        idx_sel = nomes_faixas.index(faixa_selecionada)
        caminho_faixa = dados["faixas"][idx_sel][0]

        if os.path.exists(caminho_faixa):
            with open(caminho_faixa, "rb") as f_audio:
                audio_bytes = f_audio.read()
                st.audio(audio_bytes, format="audio/mp3")

                col_btn, _ = st.columns([1, 2])
                with col_btn:
                    st.download_button(
                        label=f"⬇️ Baixar apenas esta faixa ({faixa_selecionada})",
                        data=audio_bytes,
                        file_name=faixa_selecionada,
                        mime="audio/mp3",
                        key=f"dl_single_{faixa_selecionada}"
                    )

        st.markdown("---")
        c1, c2 = st.columns(2)
        c1.metric("📦 Tamanho do Pacote ZIP", f"{tamanho_mb:.1f} MB")
        c2.metric("🎵 Total de Faixas Geradas", f"{len(dados['faixas'])}")

        with open(caminho_zip, "rb") as f_zip:
            st.download_button(
                label="⬇️ Baixar Audiobook Completo (.ZIP)",
                data=f_zip,
                file_name=os.path.basename(caminho_zip),
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )

        with st.expander("📄 Ver transcrição fonética normalizada"):
            st.text_area("Texto Processado:", value=dados["texto_limpo"][:5000] + ("..." if len(dados["texto_limpo"]) > 5000 else ""), height=220, disabled=True)
    else:
        # Se os arquivos expiraram do disco temporário, reseta a sessão de forma segura
        st.session_state.resultado_gerado = None
        st.session_state.caminho_zip_disco = None
