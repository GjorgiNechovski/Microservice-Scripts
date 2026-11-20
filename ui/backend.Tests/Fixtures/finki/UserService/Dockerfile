FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /app

RUN dotnet tool install --global dotnet-ef --version 8.0.10
ENV PATH="$PATH:/root/.dotnet/tools"

COPY UserService/*.csproj ./UserService/
WORKDIR /app/UserService
RUN dotnet restore

COPY UserService/ ./
RUN dotnet publish -c Release -o /app/out

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS runtime
WORKDIR /app/UserService

RUN dotnet tool install --global dotnet-ef --version 8.0.10
ENV PATH="$PATH:/root/.dotnet/tools"

COPY UserService/ ./

COPY --from=build /app/out ./

RUN dotnet restore

ENTRYPOINT ["sh", "-c", "dotnet ef database update --project UserService.csproj && dotnet UserService.dll"]