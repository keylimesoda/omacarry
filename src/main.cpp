#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include "backend.h"
#include "theme.h"
#include "translator.h"
int main(int argc, char *argv[]) {
    QGuiApplication app(argc, argv);
    app.setApplicationName("omacarry");
    app.setDesktopFileName("omacarry");
    QQuickStyle::setStyle("Material");
    Translator translator;
    app.installTranslator(&translator);
    Backend backend;
    Theme theme;
    QQmlApplicationEngine engine;
    QObject::connect(&translator, &Translator::changed, &engine, &QQmlEngine::retranslate);
    engine.rootContext()->setContextProperty("backend", &backend);
    engine.rootContext()->setContextProperty("theme", &theme);
    engine.load(QUrl("qrc:/Main.qml"));
    if (engine.rootObjects().isEmpty()) return 1;
    return app.exec();
}
