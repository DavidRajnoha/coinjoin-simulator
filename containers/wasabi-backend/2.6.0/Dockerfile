FROM mcr.microsoft.com/dotnet/sdk:8.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
RUN apt update && apt install -y patch

RUN mkdir -p /wasabi-src
WORKDIR /wasabi-src
RUN git clone --depth 1 --branch v2.6.0 https://github.com/WalletWasabi/WalletWasabi.git .

COPY logger.patch /wasabi-src/logger.patch
RUN patch --binary -p1 < logger.patch

COPY mining_fee_rate.patch /wasabi-src/mining_fee_rate.patch
RUN patch --binary -p1 < mining_fee_rate.patch

WORKDIR /wasabi-src/WalletWasabi.Backend
# NuGet Audit flags NU1903 for a transitive dependency pinned by this
# release and warnings are errors here, so an unchanged rebuild fails.
RUN dotnet build -c Release -p:NuGetAudit=false

# FROM mcr.microsoft.com/dotnet/runtime:8.0 # Missing ASP.NET Core
FROM mcr.microsoft.com/dotnet/aspnet:8.0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
RUN apt update && apt install -y curl

# Needs to be numeric for kubernetes security context
RUN useradd -u 1000 -ms /bin/sh wasabi
USER 1000:1000
COPY --from=0 --chown=wasabi:wasabi /wasabi-src/WalletWasabi.Backend/bin/Release/net8.0 /home/wasabi/
COPY --chown=wasabi:wasabi Config.json /home/wasabi/
COPY --chown=wasabi:wasabi run.sh /home/wasabi/
WORKDIR /home/wasabi
CMD ["./run.sh"]
