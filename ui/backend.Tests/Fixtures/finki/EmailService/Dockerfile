FROM maven:latest

WORKDIR /app

COPY pom.xml .

RUN mvn clean install -DskipTests

COPY . .

ADD https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar /otel/opentelemetry-javaagent.jar
ENV JAVA_TOOL_OPTIONS="-javaagent:/otel/opentelemetry-javaagent.jar"

EXPOSE 8080

CMD ["mvn", "spring-boot:run"]