#include <Wire.h>
#include <Adafruit_TSL2591.h>
#include <Arduino_RouterBridge.h>
#include <SoftI2C.h>
#include <Adafruit_Sensor.h>


#define SOFT_SDA 2
#define SOFT_SCL 3
#define SHT85_ADDRESS 0x44

SoftI2C softWire(SOFT_SDA, SOFT_SCL);
Adafruit_TSL2591 tsl = Adafruit_TSL2591(2591);

bool tslReady = false;

// Function Declartations
String read_sht85(String unused);
String read_light(String unused);
String read_light1(String unused);
String get_environment(String unused);
String write_serial(String data);
String read_serial(String unused);


// Redeclare SHT85 for I/O pins
bool readSHTValues(float &tempC, float &humidity){
  softWire.beginTransmission(SHT85_ADDRESS);
  softWire.write(0x24);
  softWire.write(0x00);
  if (softWire.endTransmission() != 0) {
    return false;
  }
  delay(20);
  
  int count = softWire.requestFrom(SHT85_ADDRESS, (uint8_t)6);
  if (count != 6) {
    return false;
  }
  
  uint8_t tMSB = softWire.read();
  uint8_t tLSB = softWire.read();
  softWire.read();

  uint8_t hMSB = softWire.read();
  uint8_t hLSB = softWire.read();
  softWire.read();

  uint16_t rawTemp = ((uint16_t)tMSB << 8) | tLSB;
  uint16_t rawHum = ((uint16_t)hMSB << 8) | hLSB;
  
  tempC = -45.0 + 175.0 * ((float)rawTemp / 65535.0);
  humidity = 100.0 * ((float)rawHum / 65535.0);
  
  return true;
}

// Setup
void setup() {
  Wire.begin();
  softWire.begin();
  tslReady = tsl.begin();
  
  if(!tslReady) {
    Serial.println("TSL2951 not found");
  } else {
    tsl.setGain(TSL2591_GAIN_MAX);
    tsl.setTiming(TSL2591_INTEGRATIONTIME_100MS);
  }
  
  Bridge.begin();
  Bridge.provide("read_sht85", read_sht85);
  Bridge.provide("read_light", read_light);
  Bridge.provide("read_light1", read_light1);
  Bridge.provide("get_environment", get_environment);
  Bridge.provide("write_serial", write_serial);
  Bridge.provide("read_serial", read_serial);
  Serial.begin(115200);
}

void loop() {
  // put your main code here, to run repeatedly:
}

// Sensor functions
String read_sht85(String unused) {
  
  float tempC = 0;
  float humidity = 0;
  
  if(readSHTValues(tempC, humidity)){
    
    float tempF = (tempC * 9.0 / 5.0) + 32.0;
    
    String result = "{";
    result += "\"ok\":true,";
    result += "\"temp_c\":";
    result += String(tempC, 2);
    result += ",";
    result += "\"temp_f\":";
    result += String(tempF, 2);
    result += ",";
    result += "\"humidity\":";
    result += String(humidity, 2);
    
    result += "}";
    return result;
  }
  return "{\"ok\":false,\"error\":\"SHT85_READ_FAILED\"}";
}

String read_light(String unused){
  if (tslReady) {
    return "READY";
  }
  return "TSL2591_NOT_FOUND";
}
String read_light1(String unused) {
if (!tslReady){
  return "{\"ok\":false,\"error\":\"TSL2591_NOT_FOUND\"}";
}
sensors_event_t event;
  tsl.getEvent(&event);
  uint32_t lum = tsl.getFullLuminosity();
  uint16_t ir = lum >> 16;
  uint16_t full = lum & 0xFFFF;
  uint16_t visible = full - ir;

  String json = "{";
  json += "\"ok\":true,";
  json += "\"lux\":";
  json += String (event.light, 2);
  json += ",";
  json += "\"visible\":";
  json += String(visible);
  json += ",";
  json += "\"ir\":";
  json += String(ir);
  json += ",";
  json += "\"full\":";
  json += String(full);
  json += "}";
  return json;
}
String get_environment(String unused){
  //Begining of program to get meta data
  float tempC = 0;
  float humidity = 0;
  bool shtOK = readSHTValues(tempC, humidity);
  float tempF = (tempC * 9.0 / 5.0) + 32.0;
  //Start of Json data string
  String json = "{";

  json += "\"ok\":";
  if (shtOK && tslReady) {
    json += "true";
  } else{
    json += "false";
  }
  //SHT85 data
  json += ",\"temp_c\":";
  json += String(tempC, 2);

  json += ",\"temp_f\":";
  json += String(tempF, 2);
  
  json += ",\"humidity\":";
  json += String(humidity, 2);

  //TSL2591 data
  if (tslReady) {
    sensors_event_t event;
    tsl.getEvent(&event);
    
    uint32_t lum = tsl.getFullLuminosity();
    uint16_t ir = lum >> 16;
    uint16_t full = lum & 0xFFFF;
    uint16_t visible = full - ir;

    json += ",\"lux\":";
    json += String (event.light, 2);

    json += ",\"visible\":";
    json += String(visible);
    
    json += ",\"ir\":";
    json += String(ir);
    
    json += ",\"full\":";
    json += String(full);
  } else {
    json += ",\"lux\":null";
    json += ",\"visible\":null";
    json += ",\"ir\":null";
    json += ",\"full\":null";
  }
  //End meta data  
  json += "}";
  return json;
}

// Serial bridge functions

String write_serial(String data) {
  int buffer_freespace = Serial.availableForWrite();
  if (buffer_freespace > (int)data.length()) {
    Serial.write(data.c_str(), data.length());
    return String("{\"ok\":true}");
  }
  return String("{\"ok\":false,\"error\":\"BUFFER_FULL\"}");
}

String read_serial(String unused){
  String result = String("");
  unsigned long start = millis();
  //Read up to 100ms for incoming data
  while (millis() - start < 100){
    while (Serial.available()){
      result += (char)Serial.read();
    }
  }
  return result;
}
