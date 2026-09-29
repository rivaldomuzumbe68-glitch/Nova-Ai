from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

HTML = """
<h2>Nova-Ai - Planejamento de Negocio</h2>
<p>Digite sua ideia de negocio:</p>
<input id="msg" placeholder="Ex: loja de roupa em Nampula" style="width:90%; padding:10px">
<br><br>
<button onclick="enviar()" style="padding:10px">Gerar Plano</button>
<pre id="resposta" style="white-space:pre-wrap; margin-top:20px; background:#f0f0f0; padding:10px"></pre>
<script>
async function enviar(){
  let m = document.getElementById('msg').value;
  document.getElementById('resposta').innerText = "Pensando...";
  let r = await fetch('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mensagem:m})});
  let j = await r.json();
  document.getElementById('resposta').innerText = j.resposta;
}
</script>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    ideia = request.json.get("mensagem","")
    plano = f"PLANO PARA: {ideia}\n\n1. MERCADO: Ver clientes em Nampula\n2. CUSTO INICIAL: Calcular aluguel + stock\n3. MARKETING: WhatsApp e Facebook\n4. LUCRO: Projeção de 3 meses\n\nMe diz qual parte queres detalhar?"
    return jsonify({"resposta": plano})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
