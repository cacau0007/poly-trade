from flask import Flask, render_template, jsonify

def create_app(trader):
    app = Flask(__name__, template_folder="../templates")

    @app.route("/")
    def index():
        return render_template("dashboard.html")

    @app.route("/api/status")
    def status():
        return jsonify(trader.get_status())

    @app.route("/api/start", methods=["POST"])
    def start():
        if not trader.is_running:
            trader.start()
        return jsonify({"ok": True})

    @app.route("/api/stop", methods=["POST"])
    def stop():
        trader.stop()
        return jsonify({"ok": True})

    @app.route("/api/cycle", methods=["POST"])
    def cycle():
        if trader.is_running:
            trader.run_cycle()
        return jsonify({"ok": True})

    return app
