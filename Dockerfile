FROM PYTHON:3-alpine3.12
WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt
EXPOSE 3000
CMD ["python", "system.py"]