"""Servidor de exportação do Estúdio de Sublimação.
Recebe a arte (PNG em base64), vetoriza a imagem inteira e devolve
SVG, PDF vetorial (RGB), PDF em CMYK e EPS em CMYK.
Variável de ambiente opcional: KEY (chave de acesso, enviada como Bearer)."""
import base64, io, os, subprocess, tempfile
from pathlib import Path

import cairosvg
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


def gs(args, saida):
    r = subprocess.run(["gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-q", *args, f"-sOutputFile={saida}"],
                       capture_output=True, timeout=240)
    if r.returncode != 0:
        raise HTTPException(500, "Ghostscript falhou: " + r.stderr.decode(errors="ignore")[:200])


def b64(p):
    return base64.b64encode(Path(p).read_bytes()).decode()


@app.get("/")
def saude():
    return {"ok": True}


@app.post("/vetorizar")
def vetorizar(p: Pedido, authorization: str = Header(default="")):
    chave = os.environ.get("KEY", "")
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
        cairosvg.svg2pdf(url=str(d / "arte.svg"), write_to=str(d / "rgb.pdf"), dpi=300)
        cmyk = ["-sColorConversionStrategy=CMYK", "-dProcessColorModel=/DeviceCMYK"]
        gs(["-sDEVICE=pdfwrite", *cmyk, str(d / "rgb.pdf")], d / "cmyk.pdf")
        gs(["-sDEVICE=eps2write", *cmyk, str(d / "rgb.pdf")], d / "arte.eps")
        return {"svg": b64(d / "arte.svg"), "pdf": b64(d / "rgb.pdf"),
                "pdf_cmyk": b64(d / "cmyk.pdf"), "eps": b64(d / "arte.eps")}
