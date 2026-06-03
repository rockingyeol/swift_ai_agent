FROM eclipse-temurin:21-jdk-slim AS build
WORKDIR /build
COPY pom.xml .
COPY src ./src
RUN apt-get update && apt-get install -y maven && mvn -q package -DskipTests

FROM eclipse-temurin:21-jre-slim
WORKDIR /app
COPY --from=build /build/target/prowide-svc-*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
