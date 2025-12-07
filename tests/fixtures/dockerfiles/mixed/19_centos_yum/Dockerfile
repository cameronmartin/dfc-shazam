FROM centos:7

WORKDIR /app

RUN yum install -y \
    python3 \
    python3-pip \
    gcc \
    openssl-devel \
    && yum clean all

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "app.py"]
