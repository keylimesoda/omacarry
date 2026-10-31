#pragma once
#include <QObject>
#include <QFileSystemWatcher>
#include <QTimer>
#include <QProcess>
class Theme : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString accent READ accent NOTIFY changed)
    Q_PROPERTY(int radius READ radius NOTIFY changed)
public:
    explicit Theme(QObject *parent = nullptr);
    QString accent() const { return m_accent; }
    int radius() const { return m_radius; }
signals:
    void changed();
private:
    void reload();
    QFileSystemWatcher m_watch;
    QTimer m_timer;
    QTimer m_roundingTimer;
    QProcess m_rounding;
    int m_radius = 0;
    QString m_accent = "#FFD60A";
};
