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

    console.log("---- NSFW predictions ----");
    predictions.forEach((p) => {
      console.log(`${p.className}: ${p.probability.toFixed(4)}`);
    });
    console.log("---------------------------");

    const probs = {};
    predictions.forEach((p) => {
      probs[p.className] = p.probability;
    });

    const pornScore = probs["Porn"] || 0;
    const hentaiScore = probs["Hentai"] || 0;
    const sexyScore = probs["Sexy"] || 0;
    const neutralScore = probs["Neutral"] || 0;
    const drawingScore = probs["Drawing"] || 0;
    const safeScore = neutralScore + drawingScore;
    const safeDominant = safeScore >= 0.50 || neutralScore >= 0.40 || drawingScore >= 0.40;

    let flagged = false;
    let review = false;
    let reason = null;

    // 1. High-confidence explicit content -> block.
    if (pornScore >= 0.75 || hentaiScore >= 0.75) {
      flagged = true;
      const cat = pornScore >= hentaiScore ? "Porn" : "Hentai";
      const score = Math.max(pornScore, hentaiScore);
      reason = `Explicit adult content detected (${cat}, score: ${score.toFixed(2)})`;

    // 2. Borderline explicit content -> review.
    } else if (pornScore >= 0.50 || hentaiScore >= 0.50) {
      review = true;
      const cat = pornScore >= hentaiScore ? "Porn" : "Hentai";
      const score = Math.max(pornScore, hentaiScore);
      reason = `Borderline explicit adult content (${cat}, score: ${score.toFixed(2)})`;

    // 3. Clearly safe/legal dominant content, and nothing extremely
    // suggestive -> clean.
    } else if (safeDominant && sexyScore < 0.85) {
      flagged = false;
      review = false;

    // 4. High-confidence suggestive-only content -> review. This is a
    // separate, sibling branch (not nested inside #3) so images that are
    // neither clearly safe-dominant NOR explicit still get evaluated here
    // instead of silently falling through with no reason set.
    } else if (sexyScore >= 0.85) {
      review = true;
      reason = `Borderline suggestive content (Sexy, score: ${sexyScore.toFixed(2)})`;
    }
    // else: falls through with flagged=false, review=false, reason=null - clean.

    console.log(`Flagged: ${flagged}, Review: ${review}, Reason: ${reason || 'Safe/Legal'}`);

    res.json({ flagged, review, reason, predictions });
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