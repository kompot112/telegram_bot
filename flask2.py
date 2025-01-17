from flask import Flask, request, jsonify
import openai
import os
import logging
import json
import re
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

OPENAI_API_KEY = "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"

openai.api_key = OPENAI_API_KEY

@app.route("/test_by_description", methods=["POST"])
def test_by_description():
    """
    Эндпоинт для генерации 5 тестовых (multiple-choice) вопросов по заданному описанию.
    Возвращает JSON вида:
    {
      "questions": [
        {
          "question": "Какой город - столица Франции?",
          "options": ["A) Париж", "B) Мадрид", "C) Лондон", "D) Берлин"],
          "answer": "A"
        },
        ...
      ]
    }
    """
    logging.info("Получен запрос на /test_by_description")

    if not request.is_json:
        logging.warning("Запрос не содержит JSON")
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json()
    prompt_topic = data.get("prompt", "")

    if not prompt_topic:
        logging.warning("Поле 'prompt' отсутствует в запросе (тема)")
        return jsonify({"error": "Поле 'prompt' (тема) является обязательным."}), 400

    system_prompt = (
        f"Сгенерируй 5 тестовых (multiple-choice) вопросов по описанию мероприятия '{prompt_topic}'. "
        f"У каждого вопроса должны быть ровно 4 варианта ответов (A, B, C, D) и один правильный ответ. "
        f"Верни результат строго в формате JSON, без пояснений, вида:\n\n"
        f"{{\n"
        f'  "questions": [\n'
        f"    {{\n"
        f'      "question": "...",\n'
        f'      "options": ["A) ...", "B) ...", "C) ...", "D) ..."],\n'
        f'      "answer": "A"\n'
        f"    }},\n"
        f"    ...\n"
        f"  ]\n"
        f"}}\n\n"
        "Где 'answer' - это буква одного правильного варианта."
    )

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o",  
            messages=[{"role": "user", "content": system_prompt}],
            temperature=0.7,
            max_tokens=800
        )
        gpt_raw = response.choices[0].message.content.strip()
        gpt_raw = re.sub(r'```[a-zA-Z]*', '', gpt_raw).strip()
        gpt_raw = gpt_raw.replace("```", "").strip()
        try:
            parsed = json.loads(gpt_raw)
        except json.JSONDecodeError as je:
            logging.error("Не удалось преобразовать ответ в JSON. Ответ от ChatGPT:")
            logging.error(gpt_raw)
            return jsonify({
                "error": "Ответ ChatGPT не является корректным JSON.",
                "raw_response": gpt_raw
            }), 500


        if "questions" not in parsed:
            logging.warning("В ответе нет ключа 'questions'")
            return jsonify({
                "error": "В ответе ChatGPT нет ключа 'questions'.",
                "raw_response": gpt_raw
            }), 500
        
        logging.info("Успешно сгенерированы тестовые вопросы (multiple-choice)")
        return jsonify(parsed)

    except Exception as e:
        logging.error(f"Ошибка при взаимодействии с OpenAI API: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)

