#include "port.hpp"
#include "ws185.hpp"
#include "esp_log.h"
#include "freertos/task.h"

namespace hgp {
bool ws185_reset_peripherals(i2c_master_bus_handle_t bus) {
  if (!bus) return false;
  i2c_device_config_t cfg = {};
  cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
  cfg.device_address = 0x20;
  cfg.scl_speed_hz = 400000;
  i2c_master_dev_handle_t dev = nullptr;
  if (i2c_master_bus_add_device(bus, &cfg, &dev) != ESP_OK) return false;
  hg::Ws185Resets resets(
      [dev](uint8_t reg, uint8_t& value) {
        return i2c_master_transmit_receive(dev, &reg, 1, &value, 1, 50) == ESP_OK;
      },
      [dev](uint8_t reg, uint8_t value) {
        const uint8_t data[] = {reg, value};
        return i2c_master_transmit(dev, data, sizeof(data), 50) == ESP_OK;
      },
      [](uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms)); });
  const bool ok = resets.begin();
  i2c_master_bus_rm_device(dev);
  ESP_LOGI("hg.ws185", "TCA9554 LCD/touch resets %s", ok ? "ready" : "failed");
  return ok;
}
}  // namespace hgp
