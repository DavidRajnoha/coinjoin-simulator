FROM mcr.microsoft.com/dotnet/sdk:7.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
RUN apt update && apt install -y patch

#ENV LD_DEBUG=libs

RUN mkdir -p /wasabi-src
WORKDIR /wasabi-src
RUN git clone https://github.com/zkSNACKs/WalletWasabi.git .
RUN git checkout 5157745

COPY logger2021.patch /wasabi-src/logger.patch
WORKDIR /wasabi-src
RUN patch --binary -p1 < ./logger.patch

WORKDIR /wasabi-src/WalletWasabi.Fluent.Desktop
RUN dotnet build -c Release

# FROM mcr.microsoft.com/dotnet/runtime:7.0 # Missing ASP.NET Core
FROM mcr.microsoft.com/dotnet/aspnet:7.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1

# Install SkiaSharp dependencies from the snapshot paired with this EOL base image.
RUN sed -i \
        -e 's|^# deb http://snapshot.debian.org/archive/|deb http://snapshot.debian.org/archive/|' \
        -e 's|^deb http://deb.debian.org/|# deb http://deb.debian.org/|' \
        /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y \
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
    && rm -rf /var/lib/apt/lists/*

ENV DISPLAY=:99

RUN useradd -ms /bin/sh wasabi

# Needs to be numeric for kubernetes security context
USER 1000:1000
COPY --from=0 --chown=wasabi:wasabi /wasabi-src/WalletWasabi.Fluent.Desktop/bin/Release/net7.0 /home/wasabi/
COPY --chown=wasabi:wasabi run.sh /home/wasabi/
COPY --chown=wasabi:wasabi Config.json /home/wasabi/
WORKDIR /home/wasabi

RUN ldd /home/wasabi/WalletWasabi.Fluent.Desktop

CMD Xvfb :99 -screen 0 1024x768x16 & ./run.sh
