FROM ubuntu:22.04

# Prevent interactive terminal prompt freezing
ENV DEBIAN_FRONTEND=noninteractive

# Install underlying operating system build toolkits
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Clone and compile pdf2htmlEX using its official installer scripts
RUN git clone https://github.com/opt/pdf2htmlEX
WORKDIR /opt/pdf2htmlEX
ENV UNATTENDED="-y"
RUN buildScripts/getBuildToolsApt && buildScripts/buildInstallLocallyApt

# Shift back to project sandbox directory 
WORKDIR /code

# Copy and install python service dependencies
COPY ./requirements.txt /code/requirements.txt
RUN pip3 install --no-cache-dir --upgrade -r /code/requirements.txt

# Sync application workspace files
COPY . /code

# Open container network traffic
EXPOSE 10000

# Execute FastAPI via Uvicorn server distribution
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]