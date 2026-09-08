const express = require("express");
const multer = require("multer");
const tf = require("@tensorflow/tfjs");
const nsfw = require("nsfwjs");
const sharp = require("sharp");

const app = express();
const upload = multer();
const PORT = 5001;

let model;

async function loadModel() {
  model = await nsfw.load();
  console.log("NSFWJS model loaded.");
}

async function bufferToTensor(buffer) {
  const { data, info } = await sharp(buffer)
    .removeAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });
  return tf.tensor3d(
    new Uint8Array(data),
    [info.height, info.width, info.channels],
    "int32"
  );
}

app.post("/check-image", upload.single("image"), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: "No image uploaded" });
    }

    const tensor = await bufferToTensor(req.file.buffer);
    const predictions = await model.classify(tensor);
    tensor.dispose();

    // DEBUG: log raw scores so we can see exactly what NSFWJS returned
    // and compare against the threshold below.
    console.log("---- NSFW predictions ----");
    predictions.forEach((p) => {
      console.log(`${p.className}: ${p.probability.toFixed(4)}`);
    });
    console.log("---------------------------");

    const flaggedCategories = ["Porn", "Hentai", "Sexy"];
    const flagged = predictions.some(
      (p) => flaggedCategories.includes(p.className) && p.probability > 0.6
    );

    console.log(`Flagged: ${flagged} (threshold: 0.6)`);

    res.json({ flagged, predictions });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Failed to process image" });
  }
});

loadModel().then(() => {
  app.listen(PORT, () => {
    console.log(`NSFW detection service running on http://localhost:${PORT}`);
  });
});