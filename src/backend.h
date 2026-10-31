#pragma once
#include <QObject>
#include <QVariantList>
#include <QVariantMap>

class Backend : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList plugins READ plugins NOTIFY pluginsChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY busyChanged)
    Q_PROPERTY(QString message READ message NOTIFY messageChanged)
public:
    explicit Backend(QObject *parent = nullptr);
    QVariantList plugins() const { return m_plugins; }
    bool busy() const { return m_running > 0; }
    QString message() const { return m_message; }
    Q_INVOKABLE void request(const QVariantMap &request);
signals:
    void pluginsChanged();
    void busyChanged();
    void messageChanged();
    void completed(const QString &action, const QVariantMap &result);
private:
    QVariantList m_plugins;
    int m_running = 0;
    QString m_message;
};
