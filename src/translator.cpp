#include "translator.h"
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocale>
#include <QRegularExpression>

Translator::Translator(QObject *parent) : QTranslator(parent) {
    m_debounce.setSingleShot(true);
    m_debounce.setInterval(100);
    connect(&m_watch, &QFileSystemWatcher::directoryChanged, this, [this] { m_debounce.start(); });
    connect(&m_watch, &QFileSystemWatcher::fileChanged, this, [this] { m_debounce.start(); });
    connect(&m_debounce, &QTimer::timeout, this, &Translator::reload);
    reload();
}
QString Translator::translate(const char *, const char *source, const char *, int) const {
    return m_strings.value(QString::fromUtf8(source));
}
void Translator::reload() {
    const QString path = QDir::homePath() + "/.config/omarchy/language";
    const auto paths = m_watch.files() + m_watch.directories();
    if (!paths.isEmpty()) m_watch.removePaths(paths);
    const QString dir = QFileInfo(path).absolutePath();
    if (QFileInfo::exists(dir)) m_watch.addPath(dir);
    if (QFileInfo::exists(path)) m_watch.addPath(path);
    QFile file(path);
    QString language = file.open(QIODevice::ReadOnly) ? QString::fromUtf8(file.readLine()).trimmed() : "en";
    if (!QRegularExpression("^[a-z]{2,3}(-[A-Za-z]{2})?$").match(language).hasMatch()) language = "en";
    if (language == m_language) return;
    m_language = language;
    m_strings.clear();
    QStringList languages{language.section('-', 0, 0), language};
    languages.removeDuplicates();
    for (const auto &code : languages) {
        QFile catalog(":/i18n/" + code + ".json");
        if (!catalog.open(QIODevice::ReadOnly)) continue;
        const auto obj = QJsonDocument::fromJson(catalog.readAll()).object();
        for (auto it = obj.begin(); it != obj.end(); ++it)
            if (it.value().isString()) m_strings.insert(it.key(), it.value().toString());
    }
    const QLocale locale(language);
    QLocale::setDefault(locale);
    QGuiApplication::setLayoutDirection(locale.textDirection());
    emit changed();
}
