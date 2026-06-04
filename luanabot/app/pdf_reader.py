import re
import base64
import logging
import tempfile
import os

log = logging.getLogger(__name__)


def extrair_salario_pdf(base64_data):
    try:
        # Tenta pdfplumber primeiro
        import pdfplumber
        pdf_bytes = base64.b64decode(base64_data)
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(pdf_bytes)
            fname = f.name

        texto = ''
        with pdfplumber.open(fname) as pdf:
            for page in pdf.pages:
                texto += page.extract_text() or ''
        os.unlink(fname)

        salario = extrair_valor_liquido(texto)
        if salario:
            return {'salario': salario, 'texto': texto}

        # Fallback: IA
        from claude_ai import extrair_texto_pdf_ia
        resultado = extrair_texto_pdf_ia(base64_data)
        try:
            valor = float(resultado.replace(',', '.'))
            return {'salario': valor}
        except:
            return None

    except Exception as e:
        log.error(f"Erro PDF: {e}")
        try:
            from claude_ai import extrair_texto_pdf_ia
            resultado = extrair_texto_pdf_ia(base64_data)
            valor = float(resultado.replace(',', '.'))
            return {'salario': valor}
        except:
            return None


def extrair_valor_liquido(texto):
    # Padrões comuns em recibos portugueses
    padroes = [
        r'[Ll]íquido\s*[:\.]?\s*([0-9]+[.,][0-9]{2})',
        r'[Tt]otal\s+[Ll]íquido\s*[:\.]?\s*([0-9]+[.,][0-9]{2})',
        r'[Vv]alor\s+[Ll]íquido\s*[:\.]?\s*([0-9]+[.,][0-9]{2})',
        r'[Nn]eto\s*[:\.]?\s*([0-9]+[.,][0-9]{2})',
        r'[Aa]\s+[Rr]eceber\s*[:\.]?\s*([0-9]+[.,][0-9]{2})',
        r'[Rr]emunera[çc][aã]o\s+[Ll]íquida\s*[:\.]?\s*([0-9]+[.,][0-9]{2})',
        r'TOTAL\s+LIQUIDO\.+\s*([0-9]+[.,][0-9]{2})',
    ]
    for p in padroes:
        match = re.search(p, texto)
        if match:
            valor_str = match.group(1).replace(',', '.')
            try:
                return float(valor_str)
            except:
                continue
    return None
