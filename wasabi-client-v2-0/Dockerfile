FROM mcr.microsoft.com/dotnet/sdk:6.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
RUN apt update && apt install -y patch

COPY ./wasabi-src /wasabi-src
COPY logger.patch /wasabi-src/logger.patch
WORKDIR /wasabi-src
#RUN patch --binary -p1 < logger.patch

WORKDIR /wasabi-src/WalletWasabi.Fluent.Desktop
RUN dotnet build -c Release

FROM mcr.microsoft.com/dotnet/aspnet:6.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1

# Install SkiaSharp dependencies
RUN apt-get update && apt-get install -y \
    libfontconfig1 \
    libharfbuzz0b \
    libfreetype6 \
    libx11-6 \
    libx11-dev \
    libxext6 \
    libxext-dev \
    libxrender1 \
    libxrender-dev \
    libgdiplus \
    libc6-dev \
    xvfb \
&& rm -rf /var/lib/apt/lists/* \

# Redirect output to Xvfb
RUN apt-get update && apt-get install -y xvfb
ENV DISPLAY=:99

RUN useradd -ms /bin/sh wasabi
# Needs to be numeric for kubernetes security context
USER 1000:1000
COPY --from=0 --chown=wasabi:wasabi /wasabi-src/WalletWasabi.Fluent.Desktop/bin/Release/net6.0 /home/wasabi/
COPY --chown=wasabi:wasabi Config.json /home/wasabi/
COPY --chown=wasabi:wasabi run.sh /home/wasabi/
WORKDIR /home/wasabi
CMD Xvfb :99 -screen 0 1024x768x16 & ./run.sh
