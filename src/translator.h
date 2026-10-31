#pragma once
#include <QFileSystemWatcher>
#include <QHash>
#include <QTimer>
#include <QTranslator>

class Translator : public QTranslator {
    Q_OBJECT
public:
    explicit Translator(QObject *parent = nullptr);
    bool isEmpty() const override { return false; }
    QString translate(const char *, const char *source, const char *, int) const override;
signals:
    void changed();
private:
    void reload();
    QFileSystemWatcher m_watch;
    QTimer m_debounce;
    QHash<QString, QString> m_strings;
    QString m_language;
};
