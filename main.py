"""Servidor de exportação do Estúdio de Sublimação.
Recebe a arte (PNG em base64), vetoriza a imagem inteira e devolve
SVG, PDF vetorial (RGB), PDF em CMYK e EPS em CMYK.
Variável de ambiente opcional: KEY (chave de acesso, enviada como Bearer)."""
import base64, io, os, random, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cairosvg
import httpx
import vtracer
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# px; acima disso a vetorização fica lenta, pesada e o arquivo enorme.
# No plano grátis do Render (512 MB) mantenha baixo; aumente pela variável MAX_LADO.
MAX_LADO = int(os.environ.get("MAX_LADO", "1600"))

# detalhe -> parâmetros do vtracer (mais precisão = mais curvas = arquivo maior)
PRESETS = {
    "alto":  dict(color_precision=8, layer_difference=8,  filter_speckle=2, corner_threshold=60, length_threshold=3.5),
    "medio": dict(color_precision=6, layer_difference=16, filter_speckle=4, corner_threshold=60, length_threshold=4.0),
}


class Pedido(BaseModel):
    imagem: str
    detalhe: str = "medio"


def gs(args, saida, entrada):
    # -sOutputFile precisa vir ANTES do arquivo de entrada
    r = subprocess.run(["gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-q", *args,
                        f"-sOutputFile={saida}", str(entrada)],
                       capture_output=True, timeout=240)
    if r.returncode != 0:
        raise HTTPException(500, "Ghostscript falhou: " + r.stderr.decode(errors="ignore")[:200])


def b64(p):
    return base64.b64encode(Path(p).read_bytes()).decode()



# ---------------------------------------------------------------- IA (Cloudflare Workers AI)
CF_MODELO = "@cf/black-forest-labs/flux-2-klein-9b"
BASE_PROMPT = ("pure white background, flat solid colors, no shadows, no folds, "
               "no fabric texture, no hanger, no perspective")


class PedidoIA(BaseModel):
    acao: str
    prompt: str = ""
    imagem: str | None = None
    largura: int = 0
    altura: int = 0


def _m16(n):
    return max(256, min(1920, round(n / 16) * 16))


def _medidas(l, a, padrao):
    if not l or not a:
        return padrao
    k = 1280 / max(l, a)
    return _m16(l * k), _m16(a * k)


def _chamar(modelo, prompt, imagem, w, h):
    conta = os.environ.get("CF_ACCOUNT_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    if not conta or not token:
        raise HTTPException(500, "faltam as variáveis CF_ACCOUNT_ID e CF_API_TOKEN no Render")
    # o modelo exige multipart, mesmo só com texto: por isso tudo vai em files=
    campos = {"prompt": (None, prompt), "width": (None, str(w)), "height": (None, str(h)),
              "seed": (None, str(random.randint(1, 2**31 - 1)))}
    if imagem:
        campos["input_image_0"] = ("ref.png", base64.b64decode(imagem.split(",", 1)[-1]), "image/png")
    url = f"https://api.cloudflare.com/client/v4/accounts/{conta}/ai/run/{modelo}"
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, files=campos, timeout=120)
    except Exception as e:
        raise HTTPException(502, "não consegui falar com a Cloudflare: " + str(e)[:150])
    try:
        j = r.json()
    except Exception:
        raise HTTPException(502, f"resposta inesperada da Cloudflare (status {r.status_code})")
    if r.status_code != 200 or j.get("success") is False:
        erros = j.get("errors") or []
        msg = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in erros)[:200]
        raise HTTPException(502, "Cloudflare: " + (msg or f"status {r.status_code}"))
    res = j.get("result", j)
    img = res.get("image") if isinstance(res, dict) else None
    if not img:
        raise HTTPException(502, "a Cloudflare não devolveu imagem")
    return f"data:{'image/jpeg' if img.startswith('/9j/') else 'image/png'};base64,{img}"


CF_MODELOS = [CF_MODELO, CF_MODELO, "@cf/black-forest-labs/flux-2-klein-4b"]


def gerar_imagem(prompt, imagem, w, h):
    """Tenta até 3 vezes (seed diferente a cada uma) quando o filtro de conteúdo barra o resultado."""
    ultimo = ""
    for modelo in CF_MODELOS:
        try:
            return _chamar(modelo, prompt, imagem, w, h)
        except HTTPException as e:
            ultimo = str(e.detail)
            if "flagged" not in ultimo.lower():
                raise
    raise HTTPException(502, "O filtro de conteúdo da IA barrou o resultado nas 3 tentativas. "
                             "Tente outra foto ou acrescente uma descrição. Detalhe: " + ultimo[:120])


def _tenta(f):
    try:
        return f(), None
    except HTTPException as e:
        return None, e.detail


@app.post("/ia")
def ia(p: PedidoIA, authorization: str = Header(default="")):
    chave = os.environ.get("KEY", "").strip()
    if chave and authorization != f"Bearer {chave}":
        raise HTTPException(401, "chave inválida")
    extra = p.prompt.strip()
    if p.acao == "gerar":
        if not extra:
            raise HTTPException(400, "descreva a arte que você quer criar")
        prompt = ("Flat sublimation jersey print template, front and back of the jersey laid "
                  f"completely flat side by side. Design: {extra}. {BASE_PROMPT}.")
        return {"imagem": gerar_imagem(prompt, None, 1536, 1024)}
    if not p.imagem:
        raise HTTPException(400, "envie uma imagem para esta ação")
    if p.acao == "recriar":
        prompt = ("Redraw image 0 as a flat sublimation print template: the front and the back of the "
                  "jersey laid completely flat side by side, plus both sleeves, orthographic view. "
                  "Keep exactly the same colors, patterns, logos and text as image 0. "
                  f"{BASE_PROMPT}. Clean vector illustration style."
                  + (f" Extra instructions: {extra}" if extra else ""))
        return {"imagem": gerar_imagem(prompt, p.imagem, 1536, 1024)}
    if p.acao == "corrigir":
        if not extra:
            raise HTTPException(400, "escreva o que corrigir (ex.: trocar 'NOTURNO' por 'NOTURNA')")
        w, h = _medidas(p.largura, p.altura, (1024, 1024))
        prompt = f"Edit image 0: {extra}. Keep everything else exactly identical: same layout, colors and style."
        return {"imagem": gerar_imagem(prompt, p.imagem, w, h)}
    if p.acao == "camadas":
        itens = [x.strip() for x in (extra or "logos and emblems, numbers and text").split(",") if x.strip()][:6]
        um = lambda el: gerar_imagem(
            f"From image 0, isolate only this element: {el}. Show it alone, centered, flat and "
            "front-facing, on a pure white background, nothing else, no shadows.", p.imagem, 1024, 1024)
        with ThreadPoolExecutor(max_workers=3) as ex:
            res = list(ex.map(lambda el: _tenta(lambda: um(el)), itens))
        ok = [v for v, _ in res if v]
        if not ok:
            raise HTTPException(502, str(res[0][1]))
        return {"imagens": ok}
    raise HTTPException(400, "ação desconhecida: " + p.acao)


@app.get("/")
def saude():
    return {"ok": True}


@app.post("/vetorizar")
def vetorizar(p: Pedido, authorization: str = Header(default="")):
    chave = os.environ.get("KEY", "").strip()
    if chave and authorization != f"Bearer {chave}":
        raise HTTPException(401, "chave inválida")
    try:
        dados = base64.b64decode(p.imagem.split(",", 1)[-1])
        im = Image.open(io.BytesIO(dados)).convert("RGBA")
    except Exception:
        raise HTTPException(400, "imagem inválida")
    if max(im.size) > MAX_LADO:
        im.thumbnail((MAX_LADO, MAX_LADO), Image.LANCZOS)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        im.save(d / "in.png")
        try:
            vtracer.convert_image_to_svg_py(
                str(d / "in.png"), str(d / "arte.svg"),
                colormode="color", hierarchical="stacked", mode="spline",
                max_iterations=10, splice_threshold=45, path_precision=3,
                **PRESETS.get(p.detalhe, PRESETS["medio"]))
        except Exception as e:
            raise HTTPException(500, "vetorização falhou: " + str(e)[:200])
        # dpi=300: 1 px do SVG = 1/300 pol., o mesmo tamanho do PDF do estúdio
        try:
            cairosvg.svg2pdf(url=str(d / "arte.svg"), write_to=str(d / "rgb.pdf"), dpi=300)
        except Exception as e:
            raise HTTPException(500, "conversão para PDF falhou: " + str(e)[:200])
        res = {"svg": b64(d / "arte.svg"), "pdf": b64(d / "rgb.pdf")}
        cmyk = ["-sColorConversionStrategy=CMYK", "-dProcessColorModel=/DeviceCMYK"]
        for chave, dev, saida in (("pdf_cmyk", "pdfwrite", "cmyk.pdf"), ("eps", "eps2write", "arte.eps")):
            try:
                gs([f"-sDEVICE={dev}", *cmyk], d / saida, d / "rgb.pdf")
                res[chave] = b64(d / saida)
            except Exception as e:
                res[chave] = None
                res.setdefault("erros", {})[chave] = str(getattr(e, "detail", e))[:200]
        return res
