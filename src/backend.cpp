#include "backend.h"
#include <QClipboard>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QDir>

Backend::Backend(QObject *parent) : QObject(parent) {}

void Backend::request(const QVariantMap &request) {
    const QString action = request.value("action").toString();
    const QString plugin = request.value("plugin").toString();
    auto *process = new QProcess(this);
    auto env = QProcessEnvironment::systemEnvironment();
    const QString root = env.value("OMACARRY_ROOT");
    env.insert("PYTHONPATH", root + (env.value("PYTHONPATH").isEmpty() ? "" : ":" + env.value("PYTHONPATH")));
    process->setProcessEnvironment(env);
    process->setWorkingDirectory(root);
    ++m_running;
    emit busyChanged();
    connect(process, &QProcess::finished, this, [this, process, action, plugin](int, QProcess::ExitStatus) {
        const auto parsed = QJsonDocument::fromJson(process->readAllStandardOutput());
        QVariantMap result = parsed.object().toVariantMap();
        if (result.isEmpty()) {
            result.insert("ok", false);
            result.insert("message", QString::fromUtf8(process->readAllStandardError()).trimmed());
            if (result.value("message").toString().isEmpty()) result["message"] = "Could not run the sync engine";
        }
        if (action == "scan" && result.value("ok").toBool()) {
            m_plugins = result.value("plugins").toList();
            emit pluginsChanged();
        }
        result.insert("_plugin", plugin);
        if (result.contains("clipboard")) QGuiApplication::clipboard()->setText(result.value("clipboard").toString());
        if (action != "scan" && action != "prs" && action != "login") {
            m_message = result.value("message").toString();
            emit messageChanged();
        }
        --m_running;
        emit busyChanged();
        emit completed(action, result);
        process->deleteLater();
    });
    connect(process, &QProcess::errorOccurred, this, [process](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) emit process->finished(-1, QProcess::CrashExit);
    });
    process->start("python3", {"-m", "omacarry.bridge"});
    process->write(QJsonDocument::fromVariant(request).toJson(QJsonDocument::Compact));
    process->closeWriteChannel();
}
