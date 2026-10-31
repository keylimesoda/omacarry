#include "theme.h"
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QJsonDocument>
#include <QJsonObject>

Theme::Theme(QObject *parent) : QObject(parent) {
    m_timer.setSingleShot(true);
    m_timer.setInterval(100);
    connect(&m_watch, &QFileSystemWatcher::directoryChanged, this, [this] { m_timer.start(); });
    connect(&m_watch, &QFileSystemWatcher::fileChanged, this, [this] { m_timer.start(); });
    connect(&m_timer, &QTimer::timeout, this, &Theme::reload);
    connect(&m_rounding, &QProcess::finished, this, [this](int code, QProcess::ExitStatus status) {
        const auto value = QJsonDocument::fromJson(m_rounding.readAllStandardOutput()).object().value("int");
        if (code == 0 && status == QProcess::NormalExit && value.isDouble() && value.toInt() >= 0 && value.toInt() != m_radius) {
            m_radius = value.toInt();
            emit changed();
        }
    });
    connect(&m_roundingTimer, &QTimer::timeout, this, [this] {
        if (m_rounding.state() == QProcess::NotRunning) {
            m_rounding.start("hyprctl", {"-j", "getoption", "decoration:rounding"});
            QTimer::singleShot(1000, this, [this] { if (m_rounding.state() != QProcess::NotRunning) m_rounding.kill(); });
        }
    });
    if (!qEnvironmentVariableIsEmpty("HYPRLAND_INSTANCE_SIGNATURE")) {
        m_roundingTimer.start(1500);
        m_rounding.start("hyprctl", {"-j", "getoption", "decoration:rounding"});
    }
    reload();
}
void Theme::reload() {
    const QString current = QDir::homePath() + "/.local/state/omarchy/current";
    const QString path = current + "/theme/colors.toml";
    QString accent = "#FFD60A";
    QFile file(path);
    if (file.open(QIODevice::ReadOnly)) {
        const auto match = QRegularExpression("(?m)^\\s*accent\\s*=\\s*[\\\"'](#[a-fA-F0-9]{6})[\\\"']").match(QString::fromUtf8(file.readAll()));
        if (match.hasMatch()) accent = match.captured(1);
    }
    const auto watched = m_watch.files() + m_watch.directories();
    if (!watched.isEmpty()) m_watch.removePaths(watched);
    for (const auto &candidate : {QFileInfo(current).absolutePath(), current, current + "/theme", path})
        if (QFileInfo::exists(candidate)) m_watch.addPath(candidate);
    if (accent != m_accent) { m_accent = accent; emit changed(); }
}
